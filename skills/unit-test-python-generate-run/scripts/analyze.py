#!/usr/bin/env python3
"""
analyze.py — 单测生成流水线的分析处理脚本。

子命令：
  update-state       合并 cases patch 到 test_run_state.json / 或 per-file shard
  extract-failures   打包失败上下文供 LLM 分类
  gaps               筛出需要补测的函数
  record-bug         追加源代码疑似 bug 到报告文件 / 或 per-file shard
  merge-state        合并 per-file state shards → 统一 test_run_state.json
  merge-bugs         合并 per-file bug shards → 统一 source_bugs.json

基线 (test_cases.json) 全程只读。

并行场景下，每个 sub-agent 给 update-state / record-bug 传入自己的 shard 路径
（例如 .test/state_shards/<slug>.json），主 agent 事后调用 merge-state /
merge-bugs 把它们合并到最终单文件，避免并发写冲突。

用法：
  python analyze.py update-state --baseline ... --run-state ... --cases-patch ... --round 1
  python analyze.py extract-failures --run-result ... --baseline ... --run-state ... --output ...
  python analyze.py gaps --run-result ... --baseline ... --run-state ... --output ...
  python analyze.py record-bug --bugs-file ... --file ... --function ... --case-id ... --round 1 --traceback-file ... --reason "..."
  python analyze.py merge-state --shards-dir .test/state_shards --baseline ... --output test/generated_unit/test_run_state.json
  python analyze.py merge-bugs  --shards-dir .test/bug_shards  --output .test/source_bugs.json
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

RUN_STATE_VERSION = "1.0"


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------

def _write_json_atomic(data, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(output_path)


def _load_json(path) -> dict:
    p = Path(path)
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 公共 API（供 runner.py 直接调用，无 I/O，纯数据变换）
# ---------------------------------------------------------------------------

def update_state_data(baseline: dict, run_state: dict, cases_patch: dict,
                      round_n: int, run_result: dict = None) -> dict:
    """合并 cases patch 到 run_state，可选从 run_result 同步 case 状态。
    返回更新后的 run_state（原地修改 + 返回引用）。"""
    _apply_patch(run_state, baseline, cases_patch, round_n)
    if run_result:
        _sync_case_status_from_result(run_state, run_result)
    # 追加 round entry
    rounds = run_state.setdefault("rounds", [])
    idx = next((i for i, r in enumerate(rounds) if r.get("round") == round_n), None)
    entry = _compute_round_entry(run_state, round_n)
    if idx is not None:
        rounds[idx] = entry
    else:
        rounds.append(entry)
    rounds.sort(key=lambda r: r.get("round", 0))
    return run_state


def find_gaps(run_result: dict, baseline: dict, run_state: dict) -> dict:
    """筛出需要补测的函数。返回 gaps dict（与 cmd_gaps 输出同构）。"""
    cov_config = baseline.get("coverage_config", {})
    stmt_thr = cov_config.get("statement_threshold", 90)
    branch_thr = cov_config.get("branch_threshold", 90)
    func_thr = cov_config.get("function_threshold", 100)
    exclude_dirs = cov_config.get("exclude_dirs", [])

    run_cov = run_result.get("coverage", {})
    run_state_files = run_state.get("files", {})

    gaps = []
    for src_path, finfo in baseline.get("files", {}).items():
        if any(src_path.startswith(d.rstrip("/") + "/") or src_path == d
               for d in exclude_dirs):
            continue

        file_cov = run_cov.get(src_path)
        run_state_file = run_state_files.get(src_path, {})

        for func_key, fmeta in finfo.get("functions", {}).items():
            if fmeta.get("test_optional"):
                continue
            run_func = run_state_file.get("functions", {}).get(func_key, {})
            existing_cases = run_func.get("cases", [])
            existing_summary = [
                {"id": c.get("id"), "dimension": c.get("dimension"),
                 "status": c.get("status", "pending"),
                 "failure_reason": c.get("failure_reason")}
                for c in existing_cases
            ]

            reasons = []
            missed_lines = []
            missed_branches = []
            stmt_rate = None
            branch_rate = None

            if not existing_cases:
                reasons.append("no_cases")

            if file_cov is None:
                if "no_cases" not in reasons:
                    reasons.append("no_run")
            else:
                fcov = file_cov.get("functions", {}).get(func_key)
                if fcov:
                    stmt_rate = fcov.get("statement_rate", 100.0)
                    missed_lines = fcov.get("missed_lines", [])
                    missed_branches = fcov.get("missed_branches", [])

                    if stmt_rate < stmt_thr and stmt_rate < 100.0:
                        reasons.append("low_statement")

                    file_br = file_cov.get("branch_rate", 100.0)
                    branch_rate = file_br
                    if missed_branches and file_br < branch_thr:
                        reasons.append("low_branch")

            covered_dims = set()
            for c in existing_cases:
                d = c.get("dimension")
                if d and c.get("status") in ("passed", "fixed_pending_rerun"):
                    covered_dims.add(d)
            missing_dims = [d for d in fmeta.get("dimensions", [])
                            if d not in covered_dims]

            if not reasons and not missing_dims:
                continue

            suggestions = []
            if "no_cases" in reasons:
                suggestions.append("尚未生成任何测试，按 dimensions 全量生成")
            if missing_dims:
                suggestions.append(f"补充下列维度的用例：{', '.join(missing_dims)}")
            if missed_lines:
                suggestions.append(
                    f"构造输入以覆盖源文件的未覆盖行：{missed_lines[:10]}"
                    f"{'...' if len(missed_lines) > 10 else ''}"
                )
            if missed_branches:
                suggestions.append(
                    f"构造输入以覆盖未覆盖分支（行号, 分支索引）："
                    f"{missed_branches[:10]}"
                    f"{'...' if len(missed_branches) > 10 else ''}"
                )
            if "no_run" in reasons:
                suggestions.append("测试可能未被测试框架收集，检查测试文件路径和命名")

            gaps.append({
                "file": src_path,
                "function": func_key,
                "signature": fmeta.get("signature", ""),
                "line_range": fmeta.get("line_range", []),
                "dimensions": fmeta.get("dimensions", []),
                "mocks_needed": fmeta.get("mocks_needed", []),
                "test_path": finfo.get("test_path", ""),
                "reasons": reasons or ["incomplete_dimensions"],
                "statement_rate": stmt_rate,
                "branch_rate": branch_rate,
                "missed_lines": missed_lines,
                "missed_branches": missed_branches,
                "existing_cases": existing_summary,
                "missing_dimensions": missing_dims,
                "suggestions": suggestions,
            })

    return {
        "thresholds": {"statement": stmt_thr, "branch": branch_thr, "function": func_thr},
        "overall_summary": run_result.get("summary", {}).get("coverage", {}),
        "total_gaps": len(gaps),
        "gaps": gaps,
    }


# ---------------------------------------------------------------------------
# update-state: 合并 cases patch 到 run_state
# ---------------------------------------------------------------------------

def _init_run_state(baseline: dict, baseline_path) -> dict:
    files = {}
    for src_path, finfo in baseline.get("files", {}).items():
        func_entries = {}
        for func_key, fmeta in finfo.get("functions", {}).items():
            func_entries[func_key] = {
                "func_md5_at_gen": fmeta.get("func_md5", ""),
                "cases": [],
            }
        files[src_path] = {
            "file_md5_at_gen": finfo.get("file_md5", ""),
            "test_path": finfo.get("test_path", ""),
            "functions": func_entries,
        }

    return {
        "version": RUN_STATE_VERSION,
        "baseline_ref": str(baseline_path),
        "baseline_version": baseline.get("version", ""),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "last_round": 0,
        "summary": {
            "total_cases": 0,
            "passed": 0,
            "failed": 0,
            "source_bugs": 0,
            "coverage": {
                "statement_rate": 0.0,
                "branch_rate": 0.0,
                "function_rate": 0.0,
            },
        },
        "rounds": [],
        "files": files,
    }


def _merge_cases(existing: list, incoming: list) -> list:
    by_id_existing = {c.get("id"): c for c in existing if c.get("id")}
    by_id_incoming = {c.get("id"): c for c in incoming if c.get("id")}

    merged = []
    seen = set()

    for c in incoming:
        cid = c.get("id")
        if cid:
            base = dict(by_id_existing.get(cid, {}))
            base.update(c)
            merged.append(base)
            seen.add(cid)
        else:
            merged.append(c)

    for c in existing:
        cid = c.get("id")
        if cid and cid not in seen:
            merged.append(c)
            seen.add(cid)

    return merged


def _apply_patch(run_state, baseline, patch, round_n):
    run_files = run_state.setdefault("files", {})
    baseline_files = baseline.get("files", {})

    updated_funcs = 0
    new_cases = 0

    for src_path, file_patch in patch.get("files", {}).items():
        if src_path not in baseline_files:
            print(f"警告: 基线中无 {src_path}，跳过", file=sys.stderr)
            continue

        if src_path not in run_files:
            bl_file = baseline_files[src_path]
            run_files[src_path] = {
                "file_md5_at_gen": bl_file.get("file_md5", ""),
                "test_path": bl_file.get("test_path", ""),
                "functions": {
                    k: {"func_md5_at_gen": v.get("func_md5", ""), "cases": []}
                    for k, v in bl_file.get("functions", {}).items()
                },
            }

        run_file = run_files[src_path]
        if "test_path" in file_patch:
            run_file["test_path"] = file_patch["test_path"]

        if "functions" in file_patch:
            func_patches = file_patch["functions"]
        else:
            func_patches = {
                k: v for k, v in file_patch.items()
                if k != "test_path" and isinstance(v, dict)
            }

        for func_key, func_patch in func_patches.items():
            if func_key not in baseline_files[src_path].get("functions", {}):
                print(f"警告: 基线中无 {src_path}::{func_key}，跳过", file=sys.stderr)
                continue

            if func_key not in run_file["functions"]:
                bl_func = baseline_files[src_path]["functions"][func_key]
                run_file["functions"][func_key] = {
                    "func_md5_at_gen": bl_func.get("func_md5", ""),
                    "cases": [],
                }

            rf = run_file["functions"][func_key]
            incoming = func_patch.get("cases", [])
            for c in incoming:
                c.setdefault("round_added", round_n)
                c.setdefault("status", "pending")
                c.setdefault("fix_attempts", 0)
                c.setdefault("failure_reason", None)

            before = len(rf["cases"])
            rf["cases"] = _merge_cases(rf["cases"], incoming)
            after = len(rf["cases"])
            new_cases += max(0, after - before)
            updated_funcs += 1

    total_cases = sum(
        len(f.get("cases", []))
        for fi in run_files.values()
        for f in fi.get("functions", {}).values()
    )
    run_state.setdefault("summary", {})["total_cases"] = total_cases
    run_state["last_round"] = round_n
    run_state["generated_at"] = datetime.now().isoformat(timespec="seconds")

    return {"updated_funcs": updated_funcs, "new_cases": new_cases, "total_cases": total_cases}


def _sync_case_status_from_result(run_state: dict, run_result: dict) -> int:
    """从 run_result 的 tests[] 读取测试结果，更新 run_state 中对应 case 的 status。

    映射策略：
    1. 优先用 case_id 匹配（runner.py 从 CASE_ID 注释反查的 t["case_id"]）
    2. 回退到 (test_file, bare_name) 复合 key，bare_name 会去掉 pytest 参数化后缀 [...]

    返回更新的 case 数量。
    """
    import re as _re

    # 建立 case_id → test_result 映射
    by_case_id = {}
    # 建立回退复合 key: (test_file, bare_name) → test_result
    by_composite = {}
    for t in run_result.get("tests", []):
        cid = t.get("case_id")
        if cid:
            by_case_id[cid] = t
        # 去掉 pytest 参数化后缀: test_foo[bar] → test_foo
        bare = _re.sub(r"\[.*\]$", "", t.get("name", ""))
        tf = t.get("test_file", "")
        by_composite[(tf, bare)] = t

    synced = 0
    for src_path, rs_file in run_state.get("files", {}).items():
        test_path = rs_file.get("test_path", "")
        for func_key, rs_func in rs_file.get("functions", {}).items():
            for case in rs_func.get("cases", []):
                # 优先按 case_id 查找
                t = by_case_id.get(case.get("id"))
                # 回退：按 (test_file, test_name) 复合 key
                if not t:
                    t = by_composite.get((test_path, case.get("test_name", "")))
                if not t:
                    continue

                new_status = None
                ts = t.get("status", "")
                if ts == "passed":
                    new_status = "passed"
                elif ts in ("failed", "error"):
                    new_status = "failed"
                elif ts == "skipped":
                    new_status = "skipped"

                if new_status and case.get("status") != new_status:
                    case["status"] = new_status
                    synced += 1
                    if new_status == "failed" and t.get("traceback"):
                        case.setdefault("failure_reason", t.get("failure_type"))
    return synced


def _compute_round_entry(run_state: dict, round_n: int) -> dict:
    """从当前 run_state 计算指定 round 的元数据快照。"""
    new_cases = 0
    passed = 0
    failed = 0
    source_bugs = 0
    for fi in run_state.get("files", {}).values():
        for f in fi.get("functions", {}).values():
            for c in f.get("cases", []):
                if c.get("round_added") == round_n:
                    new_cases += 1
                st = c.get("status")
                if st == "passed":
                    passed += 1
                elif st in ("failed", "failed_persistent"):
                    failed += 1
                elif st == "source_bug":
                    source_bugs += 1
    return {
        "round": round_n,
        "ended_at": datetime.now().isoformat(timespec="seconds"),
        "new_cases": new_cases,
        "passed": passed,
        "failed": failed,
        "source_bugs": source_bugs,
    }


def cmd_update_state(args):
    baseline_path = Path(args.baseline)
    run_state_path = Path(args.run_state)
    patch_path = Path(args.cases_patch)

    if not baseline_path.is_file():
        print(f"错误: 基线 {baseline_path} 不存在", file=sys.stderr)
        sys.exit(1)
    if not patch_path.is_file():
        print(f"错误: patch {patch_path} 不存在", file=sys.stderr)
        sys.exit(1)

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    if run_state_path.is_file():
        run_state = json.loads(run_state_path.read_text(encoding="utf-8"))
    else:
        run_state = _init_run_state(baseline, baseline_path)

    patch = json.loads(patch_path.read_text(encoding="utf-8"))

    stats = _apply_patch(run_state, baseline, patch, args.round)

    # 可选：从 run_result 自动同步 case 状态
    synced = 0
    if args.run_result:
        rr_path = Path(args.run_result)
        if rr_path.is_file():
            run_result = json.loads(rr_path.read_text(encoding="utf-8"))
            synced = _sync_case_status_from_result(run_state, run_result)

    # 追加 per-round 元数据
    rounds = run_state.setdefault("rounds", [])
    existing_round_idx = next(
        (i for i, r in enumerate(rounds) if r.get("round") == args.round), None
    )
    round_entry = _compute_round_entry(run_state, args.round)
    if existing_round_idx is not None:
        rounds[existing_round_idx] = round_entry
    else:
        rounds.append(round_entry)
    # 按 round 排序
    rounds.sort(key=lambda r: r.get("round", 0))

    _write_json_atomic(run_state, run_state_path)

    msg = (
        f"运行状态已更新: {run_state_path}\n"
        f"  轮数: {args.round}\n"
        f"  更新函数: {stats['updated_funcs']}\n"
        f"  新增 case: {stats['new_cases']}\n"
        f"  当前 total_cases: {stats['total_cases']}"
    )
    if synced:
        msg += f"\n  从 run_result 同步状态: {synced} 个 case"
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# extract-failures: 打包失败上下文
# ---------------------------------------------------------------------------

def _read_lines(path: Path) -> list:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _slice(lines: list, start: int, end: int, pad: int = 2) -> dict:
    n = len(lines)
    s = max(1, start - pad)
    e = min(n, end + pad)
    return {
        "start_line": s,
        "end_line": e,
        "text": "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(s, e + 1)),
    }


def _find_test_snippet(test_file: Path, test_name: str):
    if not test_file.is_file() or not test_name:
        return None
    lines = _read_lines(test_file)
    last_dot = test_name.split(".")[-1]
    py_pat = re.compile(rf"\s*def\s+{re.escape(test_name)}\s*\(")

    for i, ln in enumerate(lines, start=1):
        if py_pat.match(ln):
            indent = len(ln) - len(ln.lstrip())
            end = i
            for j in range(i + 1, len(lines) + 1):
                nxt = lines[j - 1]
                if not nxt.strip():
                    continue
                nxt_indent = len(nxt) - len(nxt.lstrip())
                if (nxt_indent <= indent and nxt.strip()
                        and not nxt.strip().startswith(("#", "@"))):
                    break
                end = j
            return _slice(lines, i, end, pad=0)
    return None


def _find_case_in_run_state(run_state: dict, case_id: str):
    for src_path, finfo in run_state.get("files", {}).items():
        for func_key, fmeta in finfo.get("functions", {}).items():
            for c in fmeta.get("cases", []):
                if c.get("id") == case_id:
                    return src_path, func_key, c
    return None


def _find_source_by_test_file(baseline: dict, test_file_rel: str):
    for src_path, finfo in baseline.get("files", {}).items():
        if finfo.get("test_path") == test_file_rel:
            return src_path, finfo
    return None


def cmd_extract_failures(args):
    run_result = _load_json(args.run_result)
    baseline = _load_json(args.baseline)
    run_state = _load_json(args.run_state)

    if not run_result or not baseline or not run_state:
        print("错误: 缺少必要的输入文件", file=sys.stderr)
        sys.exit(1)

    repo_root = Path(args.repo_root).resolve()

    failures = []
    for test in run_result.get("tests", []):
        if test.get("status") not in ("failed", "error"):
            continue

        entry = {
            "test_name": test.get("name"),
            "test_file": test.get("test_file"),
            "case_id": test.get("case_id"),
            "status": test.get("status"),
            "failure_type": test.get("failure_type"),
            "traceback": test.get("traceback") or "",
            "test_snippet": None,
            "source_file": None,
            "source_function": None,
            "source_snippet": None,
            "dimensions": None,
            "mocks_needed": None,
            "signature": None,
            "case_description": None,
            "prior_fix_attempts": 0,
            "prior_failure_reason": None,
        }

        if test.get("test_file"):
            tfile = repo_root / test["test_file"]
            if tfile.is_file():
                entry["test_snippet"] = _find_test_snippet(tfile, test.get("name") or "")

        src_path = func_key = None
        case_dict = None
        if test.get("case_id"):
            found = _find_case_in_run_state(run_state, test["case_id"])
            if found:
                src_path, func_key, case_dict = found

        if src_path and func_key:
            bl_func = (
                baseline.get("files", {}).get(src_path, {})
                .get("functions", {}).get(func_key)
            )
            if bl_func:
                entry["source_file"] = src_path
                entry["source_function"] = func_key
                entry["dimensions"] = bl_func.get("dimensions", [])
                entry["mocks_needed"] = bl_func.get("mocks_needed", [])
                entry["signature"] = bl_func.get("signature", "")

                line_range = bl_func.get("line_range", [1, 1])
                src_file = repo_root / src_path
                lines = _read_lines(src_file)
                if lines:
                    entry["source_snippet"] = _slice(lines, line_range[0], line_range[1], pad=args.pad)

                if case_dict:
                    entry["case_description"] = case_dict
                    entry["prior_fix_attempts"] = case_dict.get("fix_attempts", 0)
                    entry["prior_failure_reason"] = case_dict.get("failure_reason")

        if not entry["source_file"] and test.get("test_file"):
            fallback = _find_source_by_test_file(baseline, test["test_file"])
            if fallback:
                sp, finfo = fallback
                entry["source_file"] = sp
                entry["source_functions_in_file"] = [
                    {"key": k, "line_range": fm.get("line_range"),
                     "signature": fm.get("signature")}
                    for k, fm in finfo.get("functions", {}).items()
                ]

        failures.append(entry)

    output = {
        "generated_at": run_result.get("generated_at"),
        "total_failures": len(failures),
        "run_return_code": run_result.get("return_code"),
        "failures": failures,
    }
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"已打包 {len(failures)} 个失败 → {out_path}", file=sys.stderr)


# ---------------------------------------------------------------------------
# gaps: 筛出需要补测的函数
# ---------------------------------------------------------------------------

def cmd_gaps(args):
    run_result = _load_json(args.run_result)
    baseline = _load_json(args.baseline)
    run_state = _load_json(args.run_state)

    if not run_result or not baseline or not run_state:
        print("错误: 缺少必要的输入文件", file=sys.stderr)
        sys.exit(1)

    cov_config = baseline.get("coverage_config", {})
    stmt_thr = cov_config.get("statement_threshold", 90)
    branch_thr = cov_config.get("branch_threshold", 90)
    func_thr = cov_config.get("function_threshold", 100)
    exclude_dirs = cov_config.get("exclude_dirs", [])

    run_cov = run_result.get("coverage", {})
    run_state_files = run_state.get("files", {})

    gaps = []
    for src_path, finfo in baseline.get("files", {}).items():
        if any(src_path.startswith(d.rstrip("/") + "/") or src_path == d
               for d in exclude_dirs):
            continue

        file_cov = run_cov.get(src_path)
        run_state_file = run_state_files.get(src_path, {})

        for func_key, fmeta in finfo.get("functions", {}).items():
            # test_optional 函数跳过：不进 gap 报告
            if fmeta.get("test_optional"):
                continue
            run_func = run_state_file.get("functions", {}).get(func_key, {})
            existing_cases = run_func.get("cases", [])
            existing_summary = [
                {"id": c.get("id"), "dimension": c.get("dimension"),
                 "status": c.get("status", "pending"),
                 "failure_reason": c.get("failure_reason")}
                for c in existing_cases
            ]

            reasons = []
            missed_lines = []
            missed_branches = []
            stmt_rate = None
            branch_rate = None

            if not existing_cases:
                reasons.append("no_cases")

            if file_cov is None:
                if "no_cases" not in reasons:
                    reasons.append("no_run")
            else:
                fcov = file_cov.get("functions", {}).get(func_key)
                if fcov:
                    stmt_rate = fcov.get("statement_rate", 100.0)
                    missed_lines = fcov.get("missed_lines", [])
                    missed_branches = fcov.get("missed_branches", [])

                    if stmt_rate < stmt_thr and stmt_rate < 100.0:
                        reasons.append("low_statement")

                    file_br = file_cov.get("branch_rate", 100.0)
                    branch_rate = file_br
                    if missed_branches and file_br < branch_thr:
                        reasons.append("low_branch")

            covered_dims = set()
            for c in existing_cases:
                d = c.get("dimension")
                if d and c.get("status") in ("passed", "fixed_pending_rerun"):
                    covered_dims.add(d)
            missing_dims = [d for d in fmeta.get("dimensions", [])
                            if d not in covered_dims]

            if not reasons and not missing_dims:
                continue

            suggestions = []
            if "no_cases" in reasons:
                suggestions.append("尚未生成任何测试，按 dimensions 全量生成")
            if missing_dims:
                suggestions.append(f"补充下列维度的用例：{', '.join(missing_dims)}")
            if missed_lines:
                suggestions.append(
                    f"构造输入以覆盖源文件的未覆盖行：{missed_lines[:10]}"
                    f"{'...' if len(missed_lines) > 10 else ''}"
                )
            if missed_branches:
                suggestions.append(
                    f"构造输入以覆盖未覆盖分支（行号, 分支索引）："
                    f"{missed_branches[:10]}"
                    f"{'...' if len(missed_branches) > 10 else ''}"
                )
            if "no_run" in reasons:
                suggestions.append("测试可能未被测试框架收集，检查测试文件路径和命名")

            gaps.append({
                "file": src_path,
                "function": func_key,
                "signature": fmeta.get("signature", ""),
                "line_range": fmeta.get("line_range", []),
                "dimensions": fmeta.get("dimensions", []),
                "mocks_needed": fmeta.get("mocks_needed", []),
                "test_path": finfo.get("test_path", ""),
                "reasons": reasons or ["incomplete_dimensions"],
                "statement_rate": stmt_rate,
                "branch_rate": branch_rate,
                "missed_lines": missed_lines,
                "missed_branches": missed_branches,
                "existing_cases": existing_summary,
                "missing_dimensions": missing_dims,
                "suggestions": suggestions,
            })

    output = {
        "generated_at": run_result.get("generated_at"),
        "thresholds": {
            "statement": stmt_thr,
            "branch": branch_thr,
            "function": func_thr,
        },
        "overall_summary": run_result.get("summary", {}).get("coverage", {}),
        "total_gaps": len(gaps),
        "gaps": gaps,
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"筛出 {len(gaps)} 个需要补测的函数 → {out}\n"
        f"  阈值: stmt={stmt_thr}%, branch={branch_thr}%, func={func_thr}%",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# record-bug: 追加源代码疑似 bug
# ---------------------------------------------------------------------------

def _load_bugs(bugs_file: Path) -> dict:
    if not bugs_file.is_file():
        return {
            "version": "1.0",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "bugs": [],
        }
    try:
        return json.loads(bugs_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"警告: 现有 bug 文件无法解析（{e}），将新建", file=sys.stderr)
        return {
            "version": "1.0",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "bugs": [],
        }


def cmd_record_bug(args):
    tb_path = Path(args.traceback_file)
    if not tb_path.is_file():
        print(f"错误: traceback 文件 {tb_path} 不存在", file=sys.stderr)
        sys.exit(1)
    traceback_text = tb_path.read_text(encoding="utf-8", errors="replace")

    bugs_path = Path(args.bugs_file)
    store = _load_bugs(bugs_path)

    tb_sig = hashlib.md5(traceback_text.encode("utf-8", errors="replace")).hexdigest()[:12]
    fp = f"{args.file}::{args.function}::{args.case_id}::{tb_sig}"

    for existing in store["bugs"]:
        if existing.get("fingerprint") == fp:
            existing["last_seen_round"] = args.round
            existing["last_seen_at"] = datetime.now().isoformat(timespec="seconds")
            existing["occurrence_count"] = existing.get("occurrence_count", 1) + 1
            _write_json_atomic(store, bugs_path)
            print(f"已有记录（重复）: {fp}", file=sys.stderr)
            return

    entry = {
        "fingerprint": fp,
        "file": args.file,
        "function": args.function,
        "case_id": args.case_id,
        "first_seen_round": args.round,
        "last_seen_round": args.round,
        "first_seen_at": datetime.now().isoformat(timespec="seconds"),
        "last_seen_at": datetime.now().isoformat(timespec="seconds"),
        "occurrence_count": 1,
        "severity": args.severity,
        "reason": args.reason,
        "test_file": args.test_file,
        "test_name": args.test_name,
        "traceback": traceback_text,
    }
    store["bugs"].append(entry)
    _write_json_atomic(store, bugs_path)
    print(
        f"已登记源码 bug: {args.file}::{args.function} (case {args.case_id}) → {bugs_path}",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# merge-state: 合并 per-file state shards → 统一 run_state
# ---------------------------------------------------------------------------

def cmd_merge_state(args):
    baseline_path = Path(args.baseline)
    baseline = _load_json(baseline_path)
    if not baseline:
        print(f"错误: 基线 {args.baseline} 为空或不存在", file=sys.stderr)
        sys.exit(1)

    merged = _init_run_state(baseline, baseline_path)
    merged_files = merged["files"]

    shards_dir = Path(args.shards_dir)
    if not shards_dir.is_dir():
        print(f"警告: shards dir {shards_dir} 不存在，合并出空 run_state",
              file=sys.stderr)
        shard_paths = []
    else:
        shard_paths = sorted(shards_dir.glob("*.json"))

    max_round = 0
    for shard_path in shard_paths:
        try:
            shard = json.loads(shard_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"警告: shard {shard_path} 无法解析: {e}", file=sys.stderr)
            continue

        max_round = max(max_round, shard.get("last_round", 0))

        for src, file_data in shard.get("files", {}).items():
            if src not in merged_files:
                # 基线里没有的文件，追加（防止 shard 与基线不同步时丢数据）
                merged_files[src] = {
                    "file_md5_at_gen": file_data.get("file_md5_at_gen", ""),
                    "test_path": file_data.get("test_path", ""),
                    "functions": {},
                }

            if "test_path" in file_data:
                merged_files[src]["test_path"] = file_data["test_path"]

            for func_key, func_data in file_data.get("functions", {}).items():
                cases = func_data.get("cases", [])
                # shard 里没有 cases 的条目（skeleton）不盖真值
                if not cases:
                    merged_files[src]["functions"].setdefault(func_key, func_data)
                    continue
                merged_files[src]["functions"][func_key] = {
                    "func_md5_at_gen": func_data.get("func_md5_at_gen", ""),
                    "cases": cases,
                }

    # 重新汇总
    total_cases = 0
    passed = failed = source_bugs = 0
    for fi in merged_files.values():
        for f in fi.get("functions", {}).values():
            for c in f.get("cases", []):
                total_cases += 1
                st = c.get("status")
                if st == "passed":
                    passed += 1
                elif st in ("failed", "failed_persistent"):
                    failed += 1
                elif st in ("source_bug",):
                    source_bugs += 1

    merged["summary"]["total_cases"] = total_cases
    merged["summary"]["passed"] = passed
    merged["summary"]["failed"] = failed
    merged["summary"]["source_bugs"] = source_bugs
    merged["last_round"] = max_round
    merged["generated_at"] = datetime.now().isoformat(timespec="seconds")

    _write_json_atomic(merged, Path(args.output))
    print(
        f"已合并 {len(shard_paths)} 个 state shards → {args.output}\n"
        f"  total_cases: {total_cases} (passed={passed}, failed={failed}, "
        f"source_bugs={source_bugs})\n"
        f"  last_round: {max_round}",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# merge-bugs: 合并 per-file bug shards → 统一 source_bugs
# ---------------------------------------------------------------------------

def cmd_merge_bugs(args):
    shards_dir = Path(args.shards_dir)
    merged = {
        "version": "1.0",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "bugs": [],
    }

    by_fp = {}
    order = []
    shard_paths = sorted(shards_dir.glob("*.json")) if shards_dir.is_dir() else []

    for shard_path in shard_paths:
        try:
            shard = json.loads(shard_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"警告: shard {shard_path} 无法解析: {e}", file=sys.stderr)
            continue

        for b in shard.get("bugs", []):
            fp = b.get("fingerprint") or \
                f"{b.get('file')}::{b.get('function')}::{b.get('case_id')}"
            if fp in by_fp:
                existing = by_fp[fp]
                existing["occurrence_count"] = (
                    existing.get("occurrence_count", 1)
                    + b.get("occurrence_count", 1)
                )
                lsr = b.get("last_seen_round", 0)
                if lsr > existing.get("last_seen_round", 0):
                    existing["last_seen_round"] = lsr
                    existing["last_seen_at"] = b.get(
                        "last_seen_at", existing.get("last_seen_at")
                    )
            else:
                by_fp[fp] = dict(b)
                order.append(fp)

    merged["bugs"] = [by_fp[fp] for fp in order]
    _write_json_atomic(merged, Path(args.output))
    print(
        f"已合并 {len(shard_paths)} 个 bug shards → {args.output}；共 "
        f"{len(merged['bugs'])} 条",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="单测生成分析处理")
    sub = parser.add_subparsers(dest="command", required=True)

    # update-state
    p_us = sub.add_parser("update-state", help="合并 cases patch 到 run_state")
    p_us.add_argument("--baseline", required=True, help="test_cases.json（只读）")
    p_us.add_argument("--run-state", required=True, help="test_run_state.json")
    p_us.add_argument("--cases-patch", required=True, help="LLM 生成的 cases patch")
    p_us.add_argument("--run-result", default=None,
                      help="可选；runner.py 产出的 run_result.json，用于自动同步 case 状态")
    p_us.add_argument("--round", type=int, required=True, help="轮数")

    # extract-failures
    p_ef = sub.add_parser("extract-failures", help="打包失败上下文")
    p_ef.add_argument("--run-result", required=True)
    p_ef.add_argument("--baseline", required=True, help="基线（只读）")
    p_ef.add_argument("--run-state", required=True, help="运行状态（只读）")
    p_ef.add_argument("--repo-root", default=".")
    p_ef.add_argument("--output", required=True)
    p_ef.add_argument("--pad", type=int, default=2)

    # gaps
    p_gaps = sub.add_parser("gaps", help="筛出需要补测的函数")
    p_gaps.add_argument("--run-result", required=True)
    p_gaps.add_argument("--baseline", required=True, help="基线（只读）")
    p_gaps.add_argument("--run-state", required=True, help="运行状态（只读）")
    p_gaps.add_argument("--output", required=True)

    # record-bug
    p_rb = sub.add_parser("record-bug", help="追加源代码疑似 bug")
    p_rb.add_argument("--bugs-file", required=True,
                       help="bug 报告文件路径；并行模式下应指到 per-file shard")
    p_rb.add_argument("--file", required=True, help="源文件相对路径")
    p_rb.add_argument("--function", required=True, help="函数 key")
    p_rb.add_argument("--case-id", required=True, help="触发该 bug 的 case ID")
    p_rb.add_argument("--round", type=int, required=True, help="发现时的轮数")
    p_rb.add_argument("--traceback-file", required=True, help="traceback 文本文件")
    p_rb.add_argument("--reason", required=True, help="LLM 一句话判断")
    p_rb.add_argument("--severity", default="unknown",
                       choices=["critical", "major", "minor", "unknown"])
    p_rb.add_argument("--test-file", default="")
    p_rb.add_argument("--test-name", default="")

    # merge-state
    p_ms = sub.add_parser("merge-state",
                           help="合并 per-file state shards → 统一 run_state")
    p_ms.add_argument("--shards-dir", required=True,
                       help="存放 state shards 的目录（例如 .test/state_shards）")
    p_ms.add_argument("--baseline", required=True, help="test_cases.json 路径")
    p_ms.add_argument("--output", required=True, help="统一 run_state 输出路径")

    # merge-bugs
    p_mb = sub.add_parser("merge-bugs",
                           help="合并 per-file bug shards → 统一 source_bugs")
    p_mb.add_argument("--shards-dir", required=True,
                       help="存放 bug shards 的目录（例如 .test/bug_shards）")
    p_mb.add_argument("--output", required=True, help="统一 source_bugs 输出路径")

    args = parser.parse_args()

    if args.command == "update-state":
        cmd_update_state(args)
    elif args.command == "extract-failures":
        cmd_extract_failures(args)
    elif args.command == "gaps":
        cmd_gaps(args)
    elif args.command == "record-bug":
        cmd_record_bug(args)
    elif args.command == "merge-state":
        cmd_merge_state(args)
    elif args.command == "merge-bugs":
        cmd_merge_bugs(args)


if __name__ == "__main__":
    main()
