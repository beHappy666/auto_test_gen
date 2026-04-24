#!/usr/bin/env python3
"""
runner.py — 执行 Python 测试并采集覆盖率，输出统一结构的 run_result.json。

子命令：
  run   执行测试 + 采集覆盖率

本脚本不修改测试代码，不判断失败原因，不重跑。

Sub-agent 并行场景下，用 --test-file 和 --scope-sources 把作用域限制到
单个源文件 + 对应测试文件；--output 指向 per-file shard 路径（例如
.test/run_results/<slug>.json），避免多个 sub-agent 互相覆盖结果。

用法（全量）：
  python runner.py run --language python --repo-root . --tests test/generated_unit --source-dirs . --baseline test/generated_unit/test_cases.json --output .test/run_result.json

用法（单文件）：
  python runner.py run --language python --repo-root . --test-file test/generated_unit/core/test_parser.py --source-dirs core --scope-sources core/parser.py --baseline test/generated_unit/test_cases.json --output .test/run_results/core__parser_py.json
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

# 从 analyze.py 导入公共 API（同一目录下）
sys.path.insert(0, str(Path(__file__).parent))
from analyze import update_state_data, find_gaps, _init_run_state, _write_json_atomic as _write_json_atomic, _load_json

CASE_ID_PATTERN = re.compile(r"#\s*CASE_ID\s*:\s*([A-Za-z0-9_\-]+)")


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------

def _md5_file(path: Path) -> str:
    if not path.is_file():
        return ""
    return hashlib.md5(path.read_bytes()).hexdigest()


def _run(cmd, cwd=None, env=None, capture=True):
    kwargs = dict(cwd=cwd, env=env, shell=isinstance(cmd, str))
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc = subprocess.run(cmd, **kwargs)
    out = proc.stdout.decode("utf-8", errors="replace") if capture and proc.stdout else ""
    err = proc.stderr.decode("utf-8", errors="replace") if capture and proc.stderr else ""
    return proc.returncode, out, err


# ---------------------------------------------------------------------------
# CASE_ID 映射
# ---------------------------------------------------------------------------

def _parse_case_id_map(tests_dir: Path) -> dict:
    mapping = {}
    for root, _, files in os.walk(tests_dir):
        for fname in files:
            fpath = Path(root) / fname
            if fpath.suffix.lower() != ".py":
                continue
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            lines = text.splitlines()
            pending_case = None
            for i, line in enumerate(lines):
                m = CASE_ID_PATTERN.search(line)
                if m:
                    pending_case = m.group(1)
                    continue
                if pending_case is None:
                    continue

                match = re.match(r"\s*def\s+(test_[A-Za-z0-9_]+)\s*\(", line)
                if match:
                    mapping[(str(fpath), match.group(1))] = pending_case
                    pending_case = None
                    continue

                stripped = line.strip()
                if stripped and not stripped.startswith(("#",)):
                    if not re.match(r"\s*(def|@)", stripped):
                        pending_case = None

    return mapping


def _check_baseline_md5(baseline_path: Path, repo_root: Path):
    if not baseline_path.is_file():
        return []
    try:
        with open(baseline_path, "r", encoding="utf-8") as f:
            baseline = json.load(f)
    except Exception:
        return []

    drifts = []
    for src_path, finfo in baseline.get("files", {}).items():
        expected = finfo.get("file_md5", "")
        actual = _md5_file(repo_root / src_path)
        if expected and actual and expected != actual:
            drifts.append({"path": src_path, "expected_md5": expected, "actual_md5": actual})
    return drifts


# ---------------------------------------------------------------------------
# JUnit XML 解析
# ---------------------------------------------------------------------------

def _parse_junit_xml(xml_path: Path) -> list:
    if not xml_path.is_file():
        return []
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as e:
        print(f"警告: 无法解析 junit xml {xml_path}: {e}", file=sys.stderr)
        return []

    root = tree.getroot()
    tests = []
    suites = root.findall(".//testsuite") or [root]
    for suite in suites:
        for tc in suite.findall("testcase"):
            entry = {
                "classname": tc.get("classname", ""),
                "name": tc.get("name", ""),
                "duration_s": float(tc.get("time", "0") or 0),
                "status": "passed",
                "failure_type": None,
                "traceback": None,
                "test_file": "",
            }
            failure = tc.find("failure")
            error = tc.find("error")
            skipped = tc.find("skipped")
            if failure is not None:
                entry["status"] = "failed"
                entry["failure_type"] = failure.get("type", "AssertionError")
                entry["traceback"] = failure.text or failure.get("message", "")
            elif error is not None:
                entry["status"] = "error"
                entry["failure_type"] = error.get("type", "Error")
                entry["traceback"] = error.text or error.get("message", "")
            elif skipped is not None:
                entry["status"] = "skipped"
                entry["traceback"] = skipped.text or skipped.get("message", "")
            tests.append(entry)
    return tests


# ---------------------------------------------------------------------------
# Python: pytest + coverage.py
# ---------------------------------------------------------------------------

def _run_python(args, baseline):
    repo_root = Path(args.repo_root).resolve()
    # 单文件模式：只跑 --test-file；否则按 --tests 目录跑
    if args.test_file:
        pytest_target = Path(args.test_file).resolve()
        tests_scan_dir = pytest_target.parent if pytest_target.is_file() else pytest_target
    else:
        pytest_target = Path(args.tests).resolve()
        tests_scan_dir = pytest_target
    tool_status = {"pytest": False, "pytest_cov": False, "coverage_json": False, "xdist": False}

    py = sys.executable or "python"
    rc, _, _ = _run([py, "-c", "import pytest"])
    tool_status["pytest"] = rc == 0
    want_coverage = not args.no_coverage
    if want_coverage:
        rc, _, _ = _run([py, "-c", "import pytest_cov"])
        tool_status["pytest_cov"] = rc == 0
        rc, _, _ = _run([py, "-c", "import coverage"])
        tool_status["coverage_json"] = rc == 0
    rc, _, _ = _run([py, "-c", "import xdist"])
    tool_status["xdist"] = rc == 0
    if not tool_status["xdist"]:
        print("pytest-xdist 未安装，降级为串行执行。主 agent 应在派发前安装。", file=sys.stderr)

    if not tool_status["pytest"]:
        return {
            "language": "python",
            "error": "pytest 未安装，请 pip install pytest pytest-cov coverage",
            "tool_status": tool_status,
        }

    with tempfile.TemporaryDirectory(prefix="autogen_run_") as tmpdir:
        tmp = Path(tmpdir)
        junit_xml = tmp / "junit.xml"
        cov_data = tmp / ".coverage"
        cov_json = tmp / "coverage.json"

        # 解析 --only-cases：提前构建 case_id→test_name 映射
        k_filter = None
        if args.only_cases:
            case_ids = [c.strip() for c in args.only_cases.split(",") if c.strip()]
            only_case_map = _parse_case_id_map(tests_scan_dir)
            # 反向查找：case_id → test_name
            reverse_map = {}
            for (fpath, tname), cid in only_case_map.items():
                reverse_map.setdefault(cid, set()).add(tname)
            test_names = set()
            unresolved = []
            for cid in case_ids:
                if cid in reverse_map:
                    test_names.update(reverse_map[cid])
                else:
                    unresolved.append(cid)
            if unresolved:
                print(f"警告: --only-cases 中以下 case_id 未找到对应测试函数: {unresolved}",
                      file=sys.stderr)
            if test_names:
                # pytest -k 支持 "or" 连接多个关键词
                k_filter = " or ".join(sorted(test_names))

        cmd = [
            py, "-m", "pytest",
            str(pytest_target),
            f"--junit-xml={junit_xml}",
            "-q", "--no-header", "--tb=short",
        ]
        if args.last_failed:
            cmd.append("--lf")
        if k_filter:
            cmd.extend(["-k", k_filter])
        if tool_status["xdist"] and not args.no_parallel and not args.last_failed:
            cmd.extend(["-n", "logical"])
        env = dict(os.environ)
        if want_coverage and tool_status["pytest_cov"] and tool_status["coverage_json"]:
            source_dirs = (args.source_dirs or ".").split(",")
            for sd in source_dirs:
                sd = sd.strip()
                if sd:
                    cmd.append(f"--cov={sd}")
            cmd.extend([
                "--cov-branch",
                f"--cov-report=json:{cov_json}",
                "--cov-report=",
            ])
            env["COVERAGE_FILE"] = str(cov_data)

        print(f"$ {' '.join(cmd)}", file=sys.stderr)
        rc, out, err = _run(cmd, cwd=str(repo_root), env=env)
        print(out, file=sys.stderr)
        if err:
            print(err, file=sys.stderr)

        tests = _parse_junit_xml(junit_xml)
        case_map = _parse_case_id_map(tests_scan_dir)
        tests = _attach_case_ids_python(tests, case_map, repo_root)

        coverage = {}
        coverage_summary = {
            "statement_rate": 0.0, "branch_rate": 0.0, "function_rate": 0.0,
            "covered_statements": 0, "total_statements": 0,
            "covered_branches": 0, "total_branches": 0,
            "covered_functions": 0, "total_functions": 0,
        }
        if want_coverage and cov_json.is_file():
            coverage, coverage_summary = _parse_coverage_json(cov_json, baseline, repo_root)
            coverage, coverage_summary = _apply_scope(
                coverage, coverage_summary, args.scope_sources
            )

        summary = _summarize_tests(tests)
        summary["coverage"] = coverage_summary

        # 构建 run_result dict（后续 update-state / gaps 都需要）
        run_result = {
            "language": "python",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "return_code": rc,
            "tool_status": tool_status,
            "scope_sources": _parse_scope(args.scope_sources),
            "summary": summary,
            "tests": tests,
            "coverage": coverage,
        }

        # --- 内嵌 update-state（--cases-patch）---
        run_state_in_memory = None
        if args.cases_patch and args.run_state and baseline:
            cases_patch = _load_json(args.cases_patch)
            if cases_patch:
                run_state_path = Path(args.run_state)
                if run_state_path.is_file():
                    run_state_in_memory = _load_json(args.run_state)
                else:
                    run_state_in_memory = _init_run_state(baseline, Path(args.baseline))
                update_state_data(baseline, run_state_in_memory, cases_patch, args.round or 1, run_result)
                _write_json_atomic(run_state_in_memory, run_state_path)
                print(f"  [auto] update-state 完成 → {args.run_state}", file=sys.stderr)

        # --- 内嵌 gaps（--include-gaps）---
        gaps_data = None
        if want_coverage and args.include_gaps and baseline:
            cov = coverage_summary
            cov_config = baseline.get("coverage_config", {})
            stmt_thr = cov_config.get("statement_threshold", 90)
            branch_thr = cov_config.get("branch_threshold", 90)
            func_thr = cov_config.get("function_threshold", 100)
            not_met = (cov["statement_rate"] < stmt_thr
                       or cov["branch_rate"] < branch_thr
                       or cov["function_rate"] < func_thr)
            if not_met:
                # 复用内存中的 run_state，避免重读文件
                if run_state_in_memory is None and args.run_state:
                    run_state_in_memory = _load_json(args.run_state)
                gaps_data = find_gaps(run_result, baseline, run_state_in_memory or {})
                run_result["gaps"] = gaps_data

        # --- 增强 stderr：per-function 覆盖率明细 ---
        if want_coverage and coverage:
            scope_sources = _parse_scope(args.scope_sources)
            for src_path, fcov in coverage.items():
                if scope_sources and src_path not in scope_sources:
                    continue
                print(f"\n函数覆盖率 [{src_path}]:", file=sys.stderr)
                for func_key, func_cov in fcov.get("functions", {}).items():
                    stmt = func_cov.get("statement_rate", 0)
                    ml = len(func_cov.get("missed_lines", []))
                    mark = "✓" if ml == 0 and func_cov.get("covered") else "✗"
                    print(f"  {func_key}: stmt={stmt}% missed_lines={ml} {mark}",
                          file=sys.stderr)

        # --- 增强 stderr：gaps 摘要 ---
        if gaps_data and gaps_data.get("gaps"):
            print(f"\nGaps ({gaps_data['total_gaps']} functions):", file=sys.stderr)
            for g in gaps_data["gaps"]:
                print(f"  {g['function']}: missed_lines={g.get('missed_lines', [])[:8]}"
                      f" missing_dims={g.get('missing_dimensions', [])}", file=sys.stderr)
                for s in g.get("suggestions", [])[:2]:
                    print(f"    → {s}", file=sys.stderr)

        # --- 写精简 summary.json（供 sub-agent Read 做决策，不依赖 stderr）---
        if want_coverage and coverage:
            cov_config = (baseline or {}).get("coverage_config", {})
            stmt_thr = cov_config.get("statement_threshold", 90)
            branch_thr = cov_config.get("branch_threshold", 90)
            func_thr = cov_config.get("function_threshold", 100)

            per_function = {}
            scope_sources = _parse_scope(args.scope_sources)
            for src_path, fcov in coverage.items():
                if scope_sources and src_path not in scope_sources:
                    continue
                for func_key, func_cov in fcov.get("functions", {}).items():
                    stmt = func_cov.get("statement_rate", 0)
                    ml = func_cov.get("missed_lines", [])
                    mb = func_cov.get("missed_branches", [])
                    meets = (stmt >= stmt_thr
                             and fcov.get("branch_rate", 0) >= branch_thr
                             and (func_cov.get("covered", False)
                                  or 100.0 >= func_thr))
                    per_function[func_key] = {
                        "stmt": stmt,
                        "branch": fcov.get("branch_rate", 0),
                        "func_covered": func_cov.get("covered", False),
                        "missed_lines": ml,
                        "missed_branches": mb,
                        "meets_threshold": meets,
                    }

            summary_json = {
                "status": "ok" if rc == 0 else ("failed" if summary.get("failed") else "error"),
                "per_function": per_function,
                "gaps": [{"function": g["function"],
                          "missed_lines": g.get("missed_lines", []),
                          "missing_dimensions": g.get("missing_dimensions", []),
                          "suggestions": g.get("suggestions", [])}
                         for g in (gaps_data or {}).get("gaps", [])] if gaps_data else [],
                "fail_count": summary.get("failed", 0) + summary.get("errors", 0),
                "pass_count": summary.get("passed", 0),
                "thresholds": {"statement": stmt_thr, "branch": branch_thr, "function": func_thr},
            }
            # 写到 {output}.summary.json
            output_path = Path(args.output)
            summary_path = output_path.with_suffix(".summary.json")
            _write_json_atomic(summary_json, summary_path)

    return run_result


def _attach_case_ids_python(tests, case_map, repo_root):
    for t in tests:
        classname = t["classname"]
        name = t["name"]
        guessed_file = None
        if classname:
            rel = classname.replace(".", "/") + ".py"
            p = repo_root / rel
            if p.is_file():
                guessed_file = str(p)
        if guessed_file:
            t["test_file"] = str(Path(guessed_file).relative_to(repo_root))
            t["case_id"] = case_map.get((guessed_file, name))
        else:
            t["case_id"] = None
    return tests


def _parse_coverage_json(cov_json, baseline, repo_root):
    with open(cov_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    per_file = {}
    total_stmts = total_branches = total_funcs = 0
    cov_stmts = cov_branches = cov_funcs = 0
    baseline_files = (baseline or {}).get("files", {})

    for abs_path, finfo in data.get("files", {}).items():
        try:
            rel_path = str(Path(abs_path).resolve().relative_to(repo_root))
        except ValueError:
            rel_path = abs_path

        s_total = finfo["summary"].get("num_statements", 0)
        s_covered = finfo["summary"].get("covered_lines", 0)
        b_total = finfo["summary"].get("num_branches", 0) or 0
        b_covered = finfo["summary"].get("covered_branches", 0) or 0
        missed_lines = finfo.get("missing_lines", []) or []
        missed_branches = [tuple(x) for x in finfo.get("missing_branches", []) or []]

        func_coverage = {}
        baseline_file = baseline_files.get(rel_path, {})
        for func_key, fmeta in baseline_file.get("functions", {}).items():
            line_range = fmeta.get("line_range", [0, 0])
            start, end = line_range[0], line_range[1]
            func_lines = set(finfo.get("executed_lines", []) or []) | set(missed_lines)
            total_in_func = len([ln for ln in func_lines if start <= ln <= end])
            missed_in_func = [ln for ln in missed_lines if start <= ln <= end]
            missed_br_in_func = [b for b in missed_branches if start <= b[0] <= end]
            covered_in_func = total_in_func - len(missed_in_func)
            rate = (covered_in_func / total_in_func * 100) if total_in_func else 100.0
            func_coverage[func_key] = {
                "line_range": line_range,
                "statement_rate": round(rate, 1),
                "covered": rate == 100.0,
                "missed_lines": missed_in_func,
                "missed_branches": missed_br_in_func,
            }
            total_funcs += 1
            if rate == 100.0:
                cov_funcs += 1

        per_file[rel_path] = {
            "statement_rate": round(s_covered / s_total * 100, 1) if s_total else 100.0,
            "branch_rate": round(b_covered / b_total * 100, 1) if b_total else 100.0,
            "covered_statements": s_covered, "total_statements": s_total,
            "covered_branches": b_covered, "total_branches": b_total,
            "missed_lines": missed_lines, "missed_branches": missed_branches,
            "functions": func_coverage,
        }
        total_stmts += s_total; cov_stmts += s_covered
        total_branches += b_total; cov_branches += b_covered

    summary = {
        "statement_rate": round(cov_stmts / total_stmts * 100, 1) if total_stmts else 0.0,
        "branch_rate": round(cov_branches / total_branches * 100, 1) if total_branches else 0.0,
        "function_rate": round(cov_funcs / total_funcs * 100, 1) if total_funcs else 0.0,
        "covered_statements": cov_stmts, "total_statements": total_stmts,
        "covered_branches": cov_branches, "total_branches": total_branches,
        "covered_functions": cov_funcs, "total_functions": total_funcs,
    }
    return per_file, summary


# ---------------------------------------------------------------------------
# 作用域过滤（单文件模式）
# ---------------------------------------------------------------------------

def _parse_scope(scope_sources):
    if not scope_sources:
        return []
    return [s.strip() for s in scope_sources.split(",") if s.strip()]


def _apply_scope(coverage, summary, scope_sources):
    """把 per-file coverage 和 summary 缩到 scope_sources 指定的文件集。"""
    scope = _parse_scope(scope_sources)
    if not scope:
        return coverage, summary

    scope_set = set(scope)
    filtered = {k: v for k, v in coverage.items() if k in scope_set}

    ts = cs = tb = cb = tf = cf = 0
    for f in filtered.values():
        ts += f.get("total_statements", 0)
        cs += f.get("covered_statements", 0)
        tb += f.get("total_branches", 0)
        cb += f.get("covered_branches", 0)
        for fc in f.get("functions", {}).values():
            tf += 1
            if fc.get("covered"):
                cf += 1

    scoped_summary = {
        "statement_rate": round(cs / ts * 100, 1) if ts else 0.0,
        "branch_rate": round(cb / tb * 100, 1) if tb else 0.0,
        "function_rate": round(cf / tf * 100, 1) if tf else 0.0,
        "covered_statements": cs, "total_statements": ts,
        "covered_branches": cb, "total_branches": tb,
        "covered_functions": cf, "total_functions": tf,
    }
    return filtered, scoped_summary


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def _summarize_tests(tests):
    total = len(tests)
    passed = sum(1 for t in tests if t["status"] == "passed")
    failed = sum(1 for t in tests if t["status"] == "failed")
    errors = sum(1 for t in tests if t["status"] == "error")
    skipped = sum(1 for t in tests if t["status"] == "skipped")
    return {
        "total_tests": total,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
        "pass_rate": round(passed / total * 100, 1) if total else 0.0,
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="执行 Python 测试并采集覆盖率")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="执行测试 + 采集覆盖率")
    p_run.add_argument("--language", choices=["python"], default="python")
    p_run.add_argument("--repo-root", default=".")
    p_run.add_argument("--tests", default="test/generated_unit",
                       help="全量模式：测试目录")
    p_run.add_argument("--test-file", default=None,
                       help="单文件模式：只跑这一个测试文件（覆盖 --tests）")
    p_run.add_argument("--source-dirs", default=".",
                       help="覆盖率源目录，逗号分隔")
    p_run.add_argument("--scope-sources", default=None,
                       help="仅在报告里保留这些源文件（逗号分隔的相对路径），"
                            "并按它们重新计算 summary。适合 sub-agent 的 per-file 模式。")
    p_run.add_argument("--baseline", default=None, help="基线路径")
    p_run.add_argument("--no-coverage", action="store_true", default=False,
                       help="跳过覆盖率采集，用于 fix 循环只关心 pass/fail 的场景")
    p_run.add_argument("--no-parallel", action="store_true", default=False,
                       help="禁用 pytest-xdist 并行执行（用于 debug）")
    p_run.add_argument("--last-failed", action="store_true", default=False,
                       help="只重跑上次失败的测试（透传 pytest --lf）")
    p_run.add_argument("--only-cases", default=None,
                       help="只跑指定 case 的测试函数，逗号分隔（如 functional_01,boundary_02）。"
                            "通过 case_id→test_name 映射转为 pytest -k 表达式")
    p_run.add_argument("--cases-patch", default=None,
                       help="cases patch JSON 路径；传入后自动调 update-state 注册+同步")
    p_run.add_argument("--run-state", default=None,
                       help="state shard 路径（配合 --cases-patch 使用）")
    p_run.add_argument("--round", type=int, default=0,
                       help="当前迭代轮数（配合 --cases-patch 使用）")
    p_run.add_argument("--include-gaps", action="store_true", default=False,
                       help="覆盖率未达标时自动调 gaps，结果写入输出的 gaps 字段")
    p_run.add_argument("--output", required=True)

    args = parser.parse_args()

    baseline = None
    if args.command == "run":
        if args.baseline and Path(args.baseline).is_file():
            with open(args.baseline, "r", encoding="utf-8") as f:
                baseline = json.load(f)

        drifts = []
        if baseline:
            drifts = _check_baseline_md5(Path(args.baseline), Path(args.repo_root).resolve())
            if drifts:
                print(f"警告: {len(drifts)} 个源文件的 md5 与基线不符，建议重跑 init：",
                      file=sys.stderr)
                for d in drifts[:5]:
                    print(f"  - {d['path']}", file=sys.stderr)

        result = _run_python(args, baseline)
        result["md5_drifts"] = drifts
        _write_json_atomic(result, Path(args.output))

        summary = result.get("summary", {})
        cov = summary.get("coverage", {})
        print(
            f"\n执行完成 → {args.output}\n"
            f"  测试: {summary.get('passed', 0)}/{summary.get('total_tests', 0)} 通过 "
            f"(failed={summary.get('failed', 0)}, error={summary.get('errors', 0)})\n"
            f"  覆盖率: stmt={cov.get('statement_rate', 0)}%, "
            f"branch={cov.get('branch_rate', 0)}%, "
            f"func={cov.get('function_rate', 0)}%",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
