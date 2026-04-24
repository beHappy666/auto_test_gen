# 覆盖率迭代策略

## 达标判定

从 `{paths.run_result}` 读取覆盖率，**逐函数**检查三个指标：

```
for func_key in functions:
    cov = run_result["coverage"][source_path]["functions"][func_key]
    actual_stmt = cov["statement_rate"]
    actual_func = 100.0 if cov["covered"] else 0.0

    达标要求:
    - statement_rate >= statement_threshold
    - branch_rate >= branch_threshold（从文件级 branch_rate 或 missed_branches 推算）
    - function_rate >= function_threshold
```

**全部函数都满足三个阈值才算达标。** 哪怕只有一个函数的一条指标不满足，都必须继续迭代。

## 迭代补充策略

每次迭代不是重写，而是**补充**：

1. 调用 `analyze.py gaps` 获取精确的缺口信息
2. 针对每个缺口函数：
   - 看 `missed_lines` 和 `missed_branches`
   - 看 `missing_dimensions`
   - 看 `suggestions`
3. 为未覆盖的行/分支设计新的测试用例
4. 使用 `unittest.mock` 控制外部依赖
5. 对编排函数（`main()` 等）需逐层 mock 子函数来覆盖各分支

---

## 收益递减早停（配置驱动）

在步骤 7 检查覆盖率时，与上一轮对比。阈值从 `coverage_config` 读取：

```
no_progress_limit = coverage_config.get("no_progress_rounds", 2)

if 迭代 N >= no_progress_limit:
    delta_stmt = 本轮 statement_rate - 上轮 statement_rate
    delta_branch = 本轮 branch_rate - 上轮 branch_rate
    新增 case 通过率 = 新增 passed / 新增 total（无新增 case 时视为 0）
    if delta_stmt < 0.5 AND delta_branch < 0.5 AND 新增 case 通过率 == 0:
        → 无进展，提前终止迭代
        → 写入 unmet_reasons: "连续 {no_progress_limit} 轮无进展，提前终止"
```

每次迭代结束后记录当前覆盖率，用于下一轮比较。

## 难测函数升级（配置驱动）

检查每个缺口函数的 missed_lines 历史。阈值从 `coverage_config` 读取：

```
max_func_iters = coverage_config.get("per_function_max_iterations", 3)

if 某函数连续 max_func_iters 轮 missed_lines 无变化（集合完全相同）:
    → 标记该函数为 hard_to_test
    → 写入 unmet_reasons: "函数 {func_key} 连续 {max_func_iters} 轮 gap 未闭合，标记为 hard_to_test"
    → 后续迭代跳过该函数，不再为它生成补充测试
```

---

## 常见错误：覆盖率低但未迭代

**错误做法：**
```
迭代 1: 生成 10 个测试 → 全部通过 → 返回结果
  batch_process.py: statement=35.8%, branch=45.0%, function=20.0%
  → 未达标但 iterations_used=1  ← 这是不对的！
```

**正确做法：**
```
迭代 1: 生成 10 个测试 → 全部通过 → 检查覆盖率
  batch_process.py: statement=35.8% < 90% → 未达标
  → 调用 gaps → 发现 parse_arguments(11.1%), validate_arguments(5.0%), main(3.1%) 未覆盖
  → 继续迭代

迭代 2: 针对缺口函数生成 15 个补充测试 → 重新执行
  batch_process.py: statement=72.3% → 仍 < 90%
  → 调用 gaps → 继续

迭代 3: 再补充 10 个测试
  batch_process.py: statement=93.1%, branch=91.5%, function=100% → 达标！
  → 返回结果，iterations_used=3
```
