---
name: unit-test-python-generate-run
description: Python 单测生成流水线的生成-执行阶段：读取 init 阶段产出的只读基线 `test_cases.json`，通过文件级调度模型派发 Claude sub-agent 生成测试代码、执行测试、采集覆盖率、迭代补测。主 agent 负责调度编排和结果收集，sub-agent 负责生成-执行-覆盖率循环。所有运行状态写到 `generate_process.json` 和 `test_run_state.json`，**基线文件全程只读**。
---

# unit-test-python-generate-run

本 skill 是 Python 单测流水线的第二阶段。上游（`unit-test-gen-init`）扫描仓库产出 `test_cases.json` 基线，记录每个函数的 `dimensions` / `mocks_needed` / `func_md5` 等元数据。本 skill 负责：

1. 初始化调度状态（`generate_process.json`）
2. 按文件批量派发 Claude sub-agent
3. 每个 sub-agent 独立完成：生成测试 → 执行 → 采集覆盖率 → 失败修复 → 迭代补测
4. 收集结果，生成最终报告

支持 Python（pytest + coverage.py）。

## 硬规则

1. **基线只读**：`test_cases.json` 是 init 阶段的产物，本 skill 永远不修改它。
2. **源码不可写**：本 skill（含所有子 agent）只能在 `test/` 和 `.test/` 目录下创建/修改文件，**不得修改除 `test/` 以外的任何源码文件**。

## 主 agent 行为边界（严禁违反）

1. **严禁在 sub-agent 产物未校验通过时标 completed** — 必须验证三个 shard 文件全部存在（见步骤 6）
2. **严禁在 sub-agent 返回含 rate_limit / 429 / timeout 时立即重试** — 先标 pending，等至少 30 秒再进入下一轮 claim
3. **严禁编写测试代码** — 主 agent 只负责 dispatch / verify / merge，测试代码由 sub-agent 生成
4. **严禁在所有 sub-agent 连续失败时继续 dispatch** — 熔断后立即终止，向用户报告
5. **严禁跳过产物校验直接读 sub-agent 返回的 JSON 当结果** — sub-agent 返回的文本仅供参考

## 职责分工

- **主 agent（Claude）**：调度编排、状态管理、结果收集、生成报告。
- **子 agent（Claude sub-agent）**：生成测试代码、执行测试、分类失败、修复测试、迭代补测、返回覆盖率结果。
- **脚本**：机械性数据处理（执行测试、采集覆盖率、解析失败、合并 cases），不写测试代码也不判断失败原因。

## 前置条件

- `test/generated_unit/test_cases.json` 已由 init 阶段产生，如果没有生成则直接终止
- Python 路径：`pip install pytest pytest-cov coverage`
- 并行加速：`pip install pytest-xdist psutil`（sub-agent 首次检测到 xdist 缺失时会尝试自动安装；安装失败则降级为串行）

## 预授权路径配置（可选，减少交互弹窗）

本 skill 会读/写以下路径。建议在被测仓库根目录的 `.claude/settings.json` 中加入相应 allow 规则：

```json
{
  "permissions": {
    "allow": [
      "Read",
      "Write(test/**)",
      "Write(.test/**)",
      "Bash(python */*/scripts/runner.py*)",
      "Bash(python */*/scripts/dispatch.py*)",
      "Bash(python */*/scripts/analyze.py*)",
      "Bash(python3 */*/scripts/runner.py*)",
      "Bash(python3 */*/scripts/dispatch.py*)",
      "Bash(python3 */*/scripts/analyze.py*)",
      "Bash(pytest:*)",
      "Bash(pip install pytest-xdist*)",
      "Bash(touch .test/**/*.heartbeat)"
    ]
  }
}
```

- **读**：全仓库（扫描源码与现有测试）
- **写**：`test/generated_unit/**`（测试代码）、`.test/**`（运行状态与覆盖率）
- **Bash**：三个脚本的所有子命令、pytest 执行、xdist 安装

## 关键文件

| 路径 | 谁写 | 说明 |
|---|---|---|
| `test/generated_unit/test_cases.json` | init（只读） | 函数元数据基线 |
| `test/generated_unit/generate_process.json` | `dispatch.py`（claim 子命令原子写） | 调度状态：文件级 status + claimed_at + 子 agent 结果 |
| `test/generated_unit/test_run_state.json` | 主 agent（`analyze merge-state` 产出） | 合并后的 cases 内容 / 状态 |
| `test/generated_unit/<src>/test_<name>.py` | 子 agent | 生成的 Python 测试 |
| `.test/run_results/<slug>.json` | 子 agent（`runner.py run`） | **per-file** 测试结果 + 覆盖率 shard |
| `.test/run_results/<slug>.json.heartbeat` | 子 agent（touch） | 判活信号，mtime 超过 stale-seconds 未更新 → stale |
| `.test/state_shards/<slug>.json` | 子 agent（`analyze update-state`） | **per-file** cases shard |
| `.test/bug_shards/<slug>.json` | 子 agent（`analyze record-bug`） | **per-file** 源码 bug shard |
| `.test/source_bugs.json` | 主 agent（`analyze merge-bugs` 产出） | 合并后的源码疑似 bug |

并行正确性的关键约束：**`.test/run_results`、`.test/state_shards`、`.test/bug_shards`是每个 sub-agent 自己的 shard 目录，互不冲突。**
全局单文件（`test_run_state.json`、`.test/source_bugs.json`）只在步骤 7 之后由主 agent 调用 merge 命令一次性生成。

## 脚本结构

所有脚本位于 `scripts/` 下，按子命令组织为 3 个文件：

| 脚本 | 子命令 | 使用者 | 说明 |
|------|--------|--------|------|
| `dispatch.py` | `init` / `batch` / `claim` / `verify-artifacts` / `report` | 主 agent | 调度编排 + 产物校验 + 报告生成 |
| `runner.py` | `run` | 子 agent | 测试执行 + 覆盖率采集（支持单文件作用域） |
| `analyze.py` | `update-state` / `extract-failures` / `gaps` / `record-bug` / `merge-state` / `merge-bugs` | 子 agent（前 4 个）/ 主 agent（merge-*） | 状态管理 + 失败分析 + 缺口筛选 + bug 登记 + shard 合并 |

## 标准工作流

### 步骤 1：确认覆盖率阈值

在执行任何操作之前，读取基线的 `coverage_config` 并询问用户是否需要修改：

```bash
cat test/generated_unit/test_cases.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d.get('coverage_config',{}), indent=2))"
```

向用户展示当前阈值，询问是否修改：

> 覆盖率阈值配置（来自 test_cases.json）：
> - 语句覆盖率：90%
> - 分支覆盖率：90%
> - 函数覆盖率：100%
>
> 是否需要修改？

如果用户要求修改，用 Edit 工具直接修改 `test_cases.json` 的 `coverage_config` 字段。

### 步骤 2：环境探测与安装

在派发子 agent 之前，一次性探测并安装测试依赖，避免多个并行 sub-agent 同时 pip install 冲突：

```bash
pip install pytest pytest-cov coverage pytest-xdist psutil -q
```

> `psutil` 是 `pytest-xdist` 使用 `-n logical` 检测 CPU 核心数的依赖；缺失时 xdist 仍可运行 `-n auto`，但 `-n logical` 可能退化为串行。

验证关键包可用：

```bash
python3 -c "import pytest, pytest_cov, coverage, xdist; print('OK')"
```

如果 xdist 安装失败（网络/权限问题），sub-agent 会自动降级为串行执行，不影响功能。

### 步骤 3：初始化调度状态

如果 `generate_process.json` 已存在，可以直接进入步骤 4。

```bash
python scripts/dispatch.py init \
  --baseline test/generated_unit/test_cases.json \
  --output test/generated_unit/generate_process.json \
  --shards-root .test \
  --max-iterations 5
```

生成 `generate_process.json`，每个源文件一条记录，初始 status 为 `"pending"`。
`shards_root` 会被记住，后续 `batch / claim` 输出的 `paths.*` 都基于它。

### 步骤 4：原子 claim 一批文件

```bash
python scripts/dispatch.py claim \
  --process test/generated_unit/generate_process.json \
  --baseline test/generated_unit/test_cases.json \
  --number 3 \
  --stale-seconds 600
```

这一步同时完成选中 N 个文件并将其标注为 `"running"`，无需再用 Edit 改 JSON。返回的 JSON：

- `files[]`：与 batch 同构，每个文件带 `paths.{run_result,state_shard,bug_shard,slug}`
  —— 这些 shard 路径**必须**原样传给子 agent，否则并行时会互相覆盖
- `status_counts`：各状态计数
- `reclaimed_stale`：把超过 `--stale-seconds` 的 "running" 任务（子 agent 崩溃 / 超时）重新回收到本批
- `auto_abandoned`：`attempt_count >= 3` 且产物始终缺失，自动标记为 abandoned
- `recommended_concurrency`：AIMD 建议的下一轮并发度（1/2/3）
- `circuit_break`：是否触发熔断（连续 2 轮全 abandoned）
- `all_done`：全部进入终态时为 true

#### AIMD 节流规则

主 agent 不需要手工计算并发度。`dispatch.py claim` 内置 AIMD 算法自动调整：

| 条件 | 并发度 |
|------|--------|
| 最近 3 个完成的 sub-agent 中 ≥2 个 `attempt_count > 1` | 降到 1 |
| 最近 5 个连续 completed 且 `attempt_count == 1` | 恢复到 3 |
| 默认 | min(2, --number) |

如果 `batch_size == 0` 且 `all_done == true`，跳到步骤 8。

### 步骤 5：派发子 agent

对 claim 返回的 `files[]` 中每个文件，补充 `repo_root` / `scripts_dir` 后写盘，再并发派发 `python-test-gen-agent`。

#### 5a. 构建输入 JSON 并写盘

`dispatch.py claim` 的 `files[]` 已包含 `source_path`、`test_path`、`file_md5`、`functions`、`coverage_config`、`max_iterations`、`paths.*`。主 agent 只需补充两个路径字段：

| 补充字段 | 值 | 来源 |
|---|---|---|
| `repo_root` | 仓库绝对路径 | 主 agent 当前工作目录或用户指定 |
| `scripts_dir` | `skills/unit-test-python-generate-run/scripts` 的绝对路径 | 固定 |

将拼接后的完整 JSON 写入 `.test/<paths.slug>_input.json`（例如 `.test/core_parser_py_input.json`）。

```json
{
  "source_path": "core/parser.py",
  "test_path": "test/generated_unit/core/test_parser.py",
  "file_md5": "<source_file_md5>",
  "functions": { ... },
  "coverage_config": { ... },
  "max_iterations": 5,
  "paths": {
    "slug": "core_parser_py",
    "run_result": ".test/run_results/core_parser_py.json",
    "state_shard": ".test/state_shards/core_parser_py.json",
    "bug_shard": ".test/bug_shards/core_parser_py.json"
  },
  "repo_root": "/absolute/path/to/repo",
  "scripts_dir": "/absolute/path/to/skills/unit-test-python-generate-run/scripts"
}
```

#### 5b. 并发派发

在同一条回复里，对每个输入 JSON 用 `Agent` 工具启动 `python-test-gen-agent`（`subagent_type: "general-purpose"`）。

**提示词由三部分拼接：**

1. **Agent 定义**：Read `{repo_root}/agents/python-test-gen-agent.md` 的完整内容
2. **预加载 references**：Read 以下两个文件，拼接到 prompt 中（避免子 agent 首轮额外 Read 调用）
   - `{repo_root}/skills/unit-test-python-generate-run/references/test-code-template.md`
   - `{repo_root}/skills/unit-test-python-generate-run/references/cases-patch-format.md`
3. **任务数据**：

```
<agents/python-test-gen-agent.md 内容>

---

<references/test-code-template.md 内容>

---

<references/cases-patch-format.md 内容>

---

请处理以下测试生成任务。输入数据：
```json
<{repo_root}/.test/{slug}_input.json 的内容>
```
仓库根路径：`{repo_root}`
```

> 注：`fix-loop-guide.md` 和 `coverage-strategy.md` 仍然按需读取（不是每个 sub-agent 都需要）。

所有子 agent 并发启动（`run_in_background: false`），等待全部完成后进入步骤 6。

#### 5c. 硬性约束

- 每个文件对应一个子 agent，不可合并多个文件到一个 agent
- `paths.*` 中的 shard 路径**必须原样传入**（来自 claim 输出），不可修改——并行时子 agent 依赖各自的 shard 隔离
- 某个子 agent 失败不阻塞其他子 agent，继续等待剩余完成
- 不在此步重试失败——stale 回收由步骤 4 的 `dispatch.py claim --stale-seconds` 在下一轮自动处理
- 子 agent 返回的文本仅供参考，**步骤 6 以产物校验为准**

### 步骤 6：收集结果 + 产物校验

**产物校验（硬性，不可跳过）**：sub-agent 结束后，调用脚本校验产物：

```bash
python scripts/dispatch.py verify-artifacts \
  --process test/generated_unit/generate_process.json \
  --file <source_path> \
  --on-missing pending
```

脚本会原子检查三个 shard 文件：
1. `.test/run_results/<slug>.json`
2. `.test/state_shards/<slug>.json`
3. `.test/bug_shards/<slug>.json`（空文件也算）

返回 `verified == true` → 产物齐全，`attempt_count` 自动归零
返回 `verified == false` → 产物缺失，status 回退到 `--on-missing`（pending 或 abandoned），`last_error_category` 写入 `"no_artifact"`

**校验通过后**，对每个文件：

1. 读取 `generate_process.json`
2. 将结果写入对应文件的 `result` 字段
3. 根据 `result` 将 `status` 改为：
   - `"completed"`：`unmet_reasons == []`（达标）**或** `unmet_reasons` 非空但 `objective_blocker == true`（dead code / 抽象方法 / 不可达分支等客观原因，备注原因）
   - `"unmet"`：`unmet_reasons` 非空**且** `objective_blocker == false`——未达标原因是测试能力不足（迭代不够、测试方案问题等）
   - `"abandoned"`：所有 gap 函数都被 `source_bug` 阻塞，同时写入 `abandon_reason = "all_source_bugs"`

### 步骤 7：循环

- `circuit_break == true` → **立即停止 dispatch**，跳到步骤 8 生成报告并终止。报告需说明限流导致哪些文件 abandoned
- `recommended_concurrency == 1` 且本轮唯一的 sub-agent 也失败 → 同样跳到步骤 8，不要继续尝试
- 仍有 `"pending"` 或 stale `"running"` → 回到步骤 4（下一批，claim 会自动回收 stale + AIMD 调节并发度）
- 仍有 `"running"` 但未超时 → 等待子 agent 完成
- 全部终态 → 步骤 8

### 步骤 8：合并 shards → 生成报告

并行 shards 由主 agent 统一合并：

```bash
# 8.1 合并所有 per-file state shards
python scripts/analyze.py merge-state \
  --shards-dir .test/state_shards \
  --baseline test/generated_unit/test_cases.json \
  --output test/generated_unit/test_run_state.json

# 8.2 合并所有 per-file bug shards
python scripts/analyze.py merge-bugs \
  --shards-dir .test/bug_shards \
  --output .test/source_bugs.json

# 8.3 生成按文件的分析报告（从 .test/run_results 目录聚合覆盖率）
python scripts/dispatch.py report \
  --baseline test/generated_unit/test_cases.json \
  --run-state test/generated_unit/test_run_state.json \
  --run-results-dir .test/run_results \
  --source-bugs .test/source_bugs.json \
  --process test/generated_unit/generate_process.json \
  --output .test/per_file_report.md \
  --format markdown
```

读取报告文件并呈现给用户。同时从 `generate_process.json` 汇总：

- 达标文件数（`status == "completed"`）vs 未达标（`"unmet"`）vs 已放弃（`"abandoned"`）
- 每个文件的迭代次数（`result.iterations_used`）
- 未达标原因（`result.unmet_reasons`）和 dead code 标记（`result.dead_code`）

## generate_process.json 结构

```json
{
  "version": "1.0",
  "generated_at": "2026-04-21T12:00:00",
  "baseline_ref": "test/generated_unit/test_cases.json",
  "max_iterations": 5,
  "shards_root": ".test",
  "coverage_config": {
    "statement_threshold": 90,
    "branch_threshold": 90,
    "function_threshold": 100,
    "no_progress_rounds": 2,
    "per_function_max_iterations": 3
  },
  "files": {
    "core/parser.py": {
      "file_md5": "abc123",
      "test_path": "test/generated_unit/core/test_parser.py",
      "status": "pending",
      "claim_round": 0,
      "attempt_count": 0,
      "effective_attempt_count": 0,
      "last_error_category": null,
      "last_attempt_at": null,
      "abandon_reason": null,
      "result": null
    }
  }
}
```

| 字段 | 说明 |
|------|------|
| `claim_round` | 已被 claim 的次数（含 stale reclaim），首次 claim 时从 0 变 1 |
| `attempt_count` | 有效尝试次数（stale reclaim 不计入）；`attempt_count >= 3` 且无产物 → 自动 abandoned；产物校验通过后归零 |
| `effective_attempt_count` | 同 attempt_count（AIMD 节流看这个字段判断是否有真实重试） |
| `last_error_category` | 产物校验失败时由 `verify-artifacts` 写入：`no_artifact` |
| `last_attempt_at` | 最近一次 claim 的 ISO 时间戳 |
| `abandon_reason` | `"exhausted_attempts"`（限流/崩溃耗尽尝试）或 `"all_source_bugs"`（所有 gap 被源码 bug 阻塞） |

**注意**：`coverage_config` 中的 `no_progress_rounds` 和 `per_function_max_iterations` 可能在 `test_cases.json` 中不存在，此时 sub-agent 使用默认值（2 和 3）。`claimed_at` 和 `claim_round` 在初始状态不存在，首次 `dispatch claim` 时写入。

`status` 状态机：

```
pending ──(dispatch claim)──▶ running ──(子 agent 返回 + 产物校验通过)──▶ completed / unmet / abandoned
                                 │
                                 ├─(产物缺失 + stale 超时)──▶ pending（回收重试）
                                 │
                                 └─(attempt_count >= 3 且无产物)──▶ abandoned（熔断放弃）
```

| status | 含义 |
|---|---|
| `pending` | 还没派发过，或已被回收等待重试 |
| `running` | `dispatch claim` 已写入 `claimed_at` |
| `completed` | 子 agent 返回 `unmet_reasons == []`，达到阈值 |
| `unmet` | 未达标且原因非客观（迭代不够 / 测试方案不足），`objective_blocker == false` |
| `abandoned` | `attempt_count >= 3` 无产物（`abandon_reason = "exhausted_attempts"`），**或**所有 gap 函数都被 source_bug 阻塞（`abandon_reason = "all_source_bugs"`） |

## 子 agent 返回结果结构

```json
{
  "source_path": "core/parser.py",
  "test_path": "test/generated_unit/core/test_parser.py",
  "functions": {
    "parse_header": {
      "dimensions": ["functional", "boundary", "exception"],
      "coverage": {
        "line": { "target": 90, "actual": 95.5 },
        "branch": { "target": 90, "actual": 88.0 },
        "function": { "target": 100, "actual": 100 }
      }
    }
  },
  "unmet_reasons": ["parse_header branch 88% < 90%"],
  "objective_blocker": true,
  "dead_code": false,
  "iterations_used": 3
}
```

## 故障排查出口

- **测试全挂（导入错误）**：子 agent 在 `test/generated_unit/conftest.py` 新建最小实现
- **覆盖率为 0**：pytest-cov 未装；runner.py 会标记 `tool_status`
- **源码 md5 漂移**：runner.py 检测到漂移写入 `md5_drifts`，提醒用户重跑 init
- **子 agent 超时 / 崩溃**：不用手工清理。下一次 `dispatch claim --stale-seconds N`
  会把 `claimed_at` 早于 `now-N` 秒的"running"任务自动回收进本批，`reclaimed_stale`
  字段会列出被回收的文件路径。

## 依赖

- Python 3.9+
- `pip install pytest pytest-cov coverage`

## 参考文档

- `references/run-state-schema.md`：`test_run_state.json` 结构 + case 字段定义
- `references/run-result-schema.md`：`.test/run_result.json` 结构 + 覆盖率配置
- `references/failure-classification.md`：LLM 判定 test_code_bug / source_code_bug 的规则
