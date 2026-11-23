"""直接调用本地版 Python 参照，输出 fixture 第一关结构化报告。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PARITY_DIR = Path(__file__).resolve().parent
PANEL_PARENT = PARITY_DIR.parents[1]
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
for candidate in (str(PANEL_PARENT), str(REPOSITORY_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from panel.review_scan import load_file_detail, scan_review_listing
from panel.review_store import (
    ReviewState,
    build_export_items,
    compute_global,
    compute_progress,
    coverage_by_directory,
    decide,
    reset,
    review_status_for,
    state_to_dict,
    validate_state,
)
from panel.settings import ObsidianConfig, PanelError

EXCLUDED_ROOTS = [
    ".obsidian",
    ".trash",
    ".git",
    ".github",
    ".obsidian-git",
    "2-Permanent/Resources/MTR-P-System",
    "2-Permanent/Areas/AI-Assets/_templates",
]


def error_dict(error: PanelError) -> dict[str, Any]:
    return {"code": error.code, "message": error.message, "http_status": error.http_status}


def domain_report(script: dict[str, Any]) -> dict[str, Any]:
    state = ReviewState()
    captures: dict[str, Any] = {}
    for index, step in enumerate(script["steps"]):
        op = step["op"]
        name = step.get("name", f"{index:02d}-{op}")
        if op == "decide":
            state = decide(state, step["path"], step["action"], step["reason"], step["sha"], step["at"])
        elif op == "capture":
            captures[name] = state_to_dict(state)
        elif op == "progress":
            captures[name] = compute_progress(script["listing"], state)
        elif op == "coverage":
            captures[name] = coverage_by_directory(script["listing"], state)
        elif op == "global":
            other = decide(ReviewState(), "2-Permanent/a.md", "rejected", "other", script["sha"], "2026-01-01T00:01:00+00:00")
            captures[name] = compute_global(script["listing"], {"fixture-user": state, "other-user": other})
        elif op == "export":
            captures[name] = build_export_items(
                script["listing"], state, step["include"], lambda path: step["fresh"].get(path), user_id="fixture-user"
            )
        elif op == "status":
            captures[name] = review_status_for((state.items or {}).get(step["path"]), step["current_sha"])
        elif op == "reset":
            state = reset(state, step["path"], step["mode"], step["at"])
        elif op in {"reset_error", "decide_error"}:
            try:
                if op == "reset_error":
                    reset(state, step["path"], step["mode"], step["at"])
                else:
                    decide(state, step["path"], step["action"], step["reason"], step["sha"], step["at"])
            except PanelError as exc:
                captures[name] = error_dict(exc)
            else:
                raise AssertionError(f"预期失败但成功：{name}")
        elif op == "history_limit":
            limited = ReviewState()
            for event_index in range(205):
                limited = decide(
                    limited, "2-Permanent/a.md", "approved", "x", script["sha"],
                    f"2026-01-02T00:{event_index // 60:02d}:{event_index % 60:02d}+00:00",
                )
            captures[name] = {
                "history_length": len(limited.history or []),
                "first_at": (limited.history or [])[0].at,
                "last_at": (limited.history or [])[-1].at,
            }
        elif op == "invalid_states":
            valid = state_to_dict(state)
            invalid_cases = {
                "top_level": [],
                "unknown_field": {**valid, "extra": True},
                "record_field": {**valid, "items": {"2-Permanent/a.md": {}}},
                "nfd_path": {**valid, "items": {"2-Permanent/cafe\u0301.md": {}}},
                "bad_history": {**valid, "history": [{"path": "2-Permanent/a.md", "op": "UNDO", "from": None, "to": None, "reason": None, "content_sha256": None, "at": "2026-01-01T00:00:00+00:00"}]},
                "bad_sha": {**valid, "history": [{"path": "2-Permanent/a.md", "op": "DECIDE", "from": None, "to": "approved", "reason": None, "content_sha256": "ABC", "at": "2026-01-01T00:00:00+00:00"}]},
                "long_reason": {**valid, "history": [{"path": "2-Permanent/a.md", "op": "DECIDE", "from": None, "to": "approved", "reason": "x" * 501, "content_sha256": None, "at": "2026-01-01T00:00:00+00:00"}]},
            }
            results = {}
            for case_name, raw in invalid_cases.items():
                try:
                    validate_state(raw)
                except PanelError as exc:
                    results[case_name] = error_dict(exc)
                else:
                    raise AssertionError(f"非法状态被接受：{case_name}")
            captures[name] = results
        else:
            raise AssertionError(f"未知 domain op：{op}")
    return captures


def nist_vectors() -> list[dict[str, str | int]]:
    cases = [("empty", b""), ("abc", b"abc"), ("one_mib_zero", b"\0" * 1_048_576), ("crlf_bom", b"\xef\xbb\xbfA\r\nB\n")]
    return [
        {"name": name, "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        for name, raw in cases
    ]


def run(vault: Path) -> dict[str, Any]:
    config = ObsidianConfig(
        # 与前端一致：扫描整个库根（'.' 由 panel 适配器归一化为库根），而不是只扫单一 PARA 目录。
        vault_root=str(vault), managed_roots=(".",), excluded_roots=tuple(EXCLUDED_ROOTS),
        max_file_bytes=1_048_576, max_scan_files=5000,
    )
    listing = scan_review_listing(str(vault), config)
    source_paths = listing.pop("_source_paths", {})
    details = []
    for item in listing["items"]:
        if item["parse_status"] == "FILE_TOO_LARGE":
            details.append({"path": item["vault_relative_path"], "error": {"code": "FILE_TOO_LARGE", "message": "文件超过单文件读取上限，未读取正文。", "http_status": 413}})
            continue
        source_item = {**item, "_source_relative_path": source_paths.get(item["vault_relative_path"], item["vault_relative_path"])}
        details.append({"path": item["vault_relative_path"], "detail": load_file_detail(vault, source_item, config)})
    script = json.loads((PARITY_DIR / "domain_cases.json").read_text(encoding="utf-8"))
    return {
        "listing": listing,
        "details": details,
        "domain_case": domain_report(script),
        "nist_vectors": nist_vectors(),
        "real_vault": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    started = datetime.now(timezone.utc)
    report = run(args.vault.resolve(strict=True))
    report["run_metadata"] = {
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"engine": "python-reference", "total": report["listing"]["total"], "out": str(args.out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
