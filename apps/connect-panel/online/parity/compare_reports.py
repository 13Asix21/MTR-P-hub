"""逐字段深比对；只有显式时间戳/runner 路径白名单可豁免。"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# size_bytes 默认严格比较；不得因方便而加入豁免。
EXEMPT_SUFFIXES = (
    "listing.scanned_at",
    "listing.path",
    "listing.items[].mtime",
    "listing.items[].size_bytes",  # 仅在 --allow-platform-size 显式开启时生效，本阶段不启用。
    "details[].detail.item.mtime",
    "run_metadata.started_at",
    "run_metadata.finished_at",
)
EXPECTED_NIST = {
    "empty": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "abc": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    "one_mib_zero": "30e14955ebf1352266dc2ff8067e68104607e750abb9d3b36582b8af909fcb58",
}


def _parts(path: str) -> list[str]:
    return [part for part in path.replace("[]", "").split(".") if part]


def exempt(path: str, allow_platform_size: bool = False) -> bool:
    normalized = re.sub(r"\[\d+\]", "[]", path)
    if normalized.endswith("listing.items[].size_bytes") and not allow_platform_size:
        return False
    return any(normalized.endswith(suffix) for suffix in EXEMPT_SUFFIXES)


def compare(py: Any, js: Any, path: str = "", allow_platform_size: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    differences: list[dict[str, Any]] = []
    exemptions: list[str] = []
    if path and exempt(path, allow_platform_size):
        return differences, [path]
    if type(py) is not type(js):
        # JSON 只有 number 类型；Python 反序列化可能保留 0.0，而 JS 为 0。
        # 两种 Python 数值类型只在数值相等时等价，bool 不得借此混过。
        if isinstance(py, (int, float)) and not isinstance(py, bool) and isinstance(js, (int, float)) and not isinstance(js, bool):
            return ([{"path": path, "python": py, "javascript": js, "kind": "value"}] if py != js else []), exemptions
        return [{"path": path, "python": py, "javascript": js, "kind": "type"}], exemptions
    if isinstance(py, dict):
        for key in sorted(set(py) | set(js)):
            child = f"{path}.{key}" if path else key
            if key not in py or key not in js:
                differences.append({"path": child, "python": py.get(key, "<MISSING>"), "javascript": js.get(key, "<MISSING>"), "kind": "missing-key"})
            else:
                child_differences, child_exemptions = compare(py[key], js[key], child, allow_platform_size)
                differences.extend(child_differences)
                exemptions.extend(child_exemptions)
    elif isinstance(py, list):
        if len(py) != len(js):
            differences.append({"path": path + ".length", "python": len(py), "javascript": len(js), "kind": "length"})
        for index, (left, right) in enumerate(zip(py, js)):
            child_differences, child_exemptions = compare(left, right, f"{path}[{index}]", allow_platform_size)
            differences.extend(child_differences)
            exemptions.extend(child_exemptions)
    elif py != js:
        differences.append({"path": path, "python": py, "javascript": js, "kind": "value"})
    return differences, exemptions


def check_nist(report: dict[str, Any], label: str) -> tuple[list[dict[str, Any]], int]:
    # 前三个固定值取 FIPS/常见标准向量；全部固定值均独立于被测 JS 实现。
    expected = dict(EXPECTED_NIST)
    import hashlib
    expected["crlf_bom"] = hashlib.sha256(b"\xef\xbb\xbfA\r\nB\n").hexdigest()
    failures = []
    vectors = report.get("nist_vectors", [])
    for vector in vectors:
        if vector["sha256"] != expected[vector["name"]]:
            failures.append({"engine": label, "vector": vector["name"], "expected": expected[vector["name"]], "actual": vector["sha256"]})
    if len(vectors) != len(expected):
        failures.append({"engine": label, "vector_count": len(vectors), "expected_count": len(expected)})
    return failures, len(vectors)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("python_report", type=Path)
    parser.add_argument("javascript_report", type=Path)
    parser.add_argument("--expected-total", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--allow-platform-size", action="store_true", help="架构保留开关；第一关默认禁用")
    args = parser.parse_args()
    py = json.loads(args.python_report.read_text(encoding="utf-8"))
    js = json.loads(args.javascript_report.read_text(encoding="utf-8"))
    differences, exemptions = compare(py, js, allow_platform_size=args.allow_platform_size)
    nist_failures = []
    nist_count = 0
    for label, report in (("python", py), ("javascript", js)):
        failures, count = check_nist(report, label)
        nist_failures.extend(failures)
        nist_count += count
    totals = {"python": py["listing"]["total"], "javascript": js["listing"]["total"], "expected": args.expected_total}
    if len({*totals.values()}) != 1:
        differences.append({"path": "listing.total", "kind": "expected-total", **totals})
    report = {
        "schema_version": "1",
        "gate": "T02-fixture-only",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "expected_fixture_total": args.expected_total,
        "fixture_totals": totals,
        "difference_count": len(differences),
        "total_differences": len(differences),
        "differences": differences,
        "exempted_count": len(exemptions),
        "exempted_paths": exemptions,
        "exemption_policy": "Only scanned_at, runner absolute path and mtime; size_bytes strict by default.",
        "nist": {"passed": not nist_failures, "vector_results": nist_count, "failures": nist_failures},
        "real_vault_full_parity_performed": False,
        "passed": not differences and not nist_failures,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"passed": report["passed"], "differences": len(differences), "nist_passed": not nist_failures, "out": str(args.out)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
