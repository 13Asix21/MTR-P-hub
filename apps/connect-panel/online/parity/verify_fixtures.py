"""独立 Python 单测：fixture 工厂重跑的字节确定性。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

PARITY = Path(__file__).resolve().parent


def digest(root: Path) -> str:
    result = hashlib.sha256()
    for path in sorted((path for path in root.rglob("*") if path.is_file()), key=lambda path: path.as_posix()):
        result.update(path.relative_to(root).as_posix().encode("utf-8"))
        result.update(b"\0")
        result.update(hashlib.sha256(path.read_bytes()).digest())
    return result.hexdigest()


def main() -> int:
    fixtures = PARITY / "fixtures"
    before = digest(fixtures)
    manifest_before = json.loads((fixtures / "fixtures_manifest.json").read_text(encoding="utf-8"))
    for _iteration in range(2):
        subprocess.run([sys.executable, str(PARITY / "build_fixture.py")], check=True, capture_output=True, text=True)
        assert digest(fixtures) == before, "fixture 工厂重跑出现字节差异"
        manifest_after = json.loads((fixtures / "fixtures_manifest.json").read_text(encoding="utf-8"))
        assert manifest_after == manifest_before, "fixture manifest 重跑出现内容差异"
    report = {
        "schema_version": "1",
        "passed": True,
        "runs": 2,
        "fixture_tree_sha256": manifest_before["fixture_tree_sha256"],
        "fixture_manifest_sha256": hashlib.sha256((fixtures / "fixtures_manifest.json").read_bytes()).hexdigest(),
        "stage2_reference": "read-only",
    }
    (PARITY / "reports" / "fixture_determinism.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
