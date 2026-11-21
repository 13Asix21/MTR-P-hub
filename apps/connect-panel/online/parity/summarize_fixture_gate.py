"""汇总扩展与 stage2 两套 fixture 的第一关结果；不扩大扫描范围。"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    """读取 UTF-8 JSON 对象。"""
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError(f"报告必须是 JSON 对象：{path}")
    return result


def summarize(reports: Path) -> dict[str, Any]:
    """核验两套报告均通过后生成联合汇总，保留各自独立证据。"""
    suites: list[dict[str, Any]] = []
    for name, prefix in (("synthetic-extended", "extended_"), ("stage2-read-only", "stage2_")):
        parity = read_json(reports / f"{prefix}parity_report.json")
        differences = read_json(reports / f"{prefix}diff_report.json")
        if not parity["passed"] or not differences["passed"]:
            raise ValueError(f"{name} 尚未通过，拒绝生成全绿汇总。")
        if parity["difference_count"] != 0 or differences["difference_count"] != 0:
            raise ValueError(f"{name} 存在未解决差异。")
        if not differences["nist"]["passed"]:
            raise ValueError(f"{name} 存在 NIST 失败向量。")
        suites.append({
            "fixture_name": name,
            "fixture_count": parity["fixture_count"],
            "difference_count": differences["difference_count"],
            "exempted_count": differences["exempted_count"],
            "nist_passed": differences["nist"]["passed"],
            "nist_vector_results": differences["nist"]["vector_results"],
            "parity_report": f"{prefix}parity_report.json",
            "diff_report": f"{prefix}diff_report.json",
            "engine_commands": [command["command"] for command in parity["commands"]],
            "exit_codes": [command["exit_code"] for command in parity["commands"]],
        })
    total = sum(suite["fixture_count"] for suite in suites)
    return {
        "schema_version": "1",
        "stage": "T01-T02 fixture gate",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fixture_count": total,
        "suites": suites,
        "difference_count": sum(suite["difference_count"] for suite in suites),
        "nist_passed": all(suite["nist_passed"] for suite in suites),
        "nist_vector_results": sum(suite["nist_vector_results"] for suite in suites),
        "size_bytes_strict": True,
        "real_vault_full_parity_performed": False,
        "passed": total == 50,
    }


def main() -> int:
    """命令行入口；两套套件全绿且总数为 50 才返回零。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports", type=Path, default=Path(__file__).resolve().parent / "reports")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = summarize(args.reports)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
