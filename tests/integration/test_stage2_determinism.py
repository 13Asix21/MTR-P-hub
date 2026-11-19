"""Three-independent-process deterministic Fixture output including continuation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.conftest import ROOT


def test_three_processes_with_decision_continuation_are_byte_identical(config_dir: Path) -> None:
    outputs: list[bytes] = []
    for seed in ("1", "77", "random"):
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = seed
        env["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "mtrp_hub",
                "query",
                "fixture",
                "--config-dir",
                str(config_dir),
                "--query",
                "deterministic stage2 query",
                "--intent",
                "decision_history",
                "--project",
                "example-project",
                "--repo",
                "example-repo",
                "--fixture",
                "tests/fixtures/stage2/stage2-decision-history.json",
                "--format",
                "json",
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
        outputs.append(completed.stdout)
    assert outputs[0] == outputs[1] == outputs[2]
    payload = json.loads(outputs[0])
    assert [step["step_id"] for step in payload["step_results"]] == ["s01-p", "s02-t", "s03-m"]
    assert "run_seq" not in outputs[0].decode() and "duration_ms" not in outputs[0].decode()
