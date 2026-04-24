# Fix 循环指南

## 失败分类决策表

读取 `extract-failures` 输出的 failures JSON，对每个 failure 按以下规则分类：

| 分类 | 条件 | 处理 |
|------|------|------|
| `test_code_bug` | import 错误、断言写错、mock 配置错误、fixture 缺失 | Edit 修复测试代码 |
| `source_code_bug` | 源代码逻辑错误（传入合法输入但函数返回错误结果） | 调用 `analyze.py record-bug` 登记 |
| `ambiguous` | 不确定归属 | 先按 test_code_bug 修复，二次仍失败则升级为 source_code_bug |

### 常见 test_code_bug 模式

- `ModuleNotFoundError` / `ImportError` → import 路径写错
- `AssertionError` 且被测函数逻辑简单 → 断言值写错
- `TypeError: missing required argument` → mock 配置不完整
- `AttributeError: Mock object has no attribute` → mock target 错误或 return_value 链不完整

### 常见 source_code_bug 模式

- 函数在特定边界条件下返回错误值（如 `None` 而非空字符串）
- 异常处理分支缺失（如未捕获 `IndexError`）
- 类型转换错误（如 `int()` 接收空字符串）

## 修复证据链

每次修复时，在 cases patch 中记录修复上下文：

```json
{
  "id": "functional_01",
  "status": "fixed_pending_rerun",
  "fix_attempts": 2,
  "last_fix_diff": "将 assert result == None 改为 assert result is None",
  "last_traceback_summary": "AssertionError: None != None (is vs ==)"
}
```

| 字段 | 说明 |
|------|------|
| `fix_attempts` | 当前已修复次数 |
| `last_fix_diff` | 一句话描述改了什么（如 "mock 了 subprocess.run"、"修正了 import 路径"） |
| `last_traceback_summary` | 上次的错误摘要 |

### 二次修复策略

**第二次修复前**：先比对 `last_fix_diff` 和当前错误。

- 如果错误**相同**（traceback 摘要一致）→ 直接升级为 `source_code_bug`，不要重复同样的修复
- 如果错误**不同**但 fix 策略类似（如连续两次都是 "改了 mock"）→ 也升级为 `source_code_bug`
- 如果错误**不同**且 fix 策略不同 → 允许继续按 `test_code_bug` 修复

### 修复次数限制

- 同一个 case 最多修复 **3 次**
- 超过 3 次自动登记为 `source_code_bug`

## record-bug 命令

```bash
python {scripts_dir}/analyze.py record-bug \
  --bugs-file {paths.bug_shard} \
  --file <source_path> \
  --function <func_name> \
  --case-id <case_id> \
  --round {N} \
  --traceback-file /tmp/tb_1.txt \
  --reason "一句话描述 bug 原因"
```

**`--bugs-file` 必须指向 `paths.bug_shard`**，不要写全局 `.test/source_bugs.json`。
