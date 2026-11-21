# -*- coding: utf-8 -*-
"""L01 全套回归的隔离入口：只把 QA 支撑路径换成合成临时档案。

不复制、不读取真实 data/users.json；不导入既有 conftest.py。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

import pytest
from ruamel.yaml import YAML


# 只影响 conftest 的既有 session 服务；独立L01用例仍使用自己的临时配置/旧档。
os.environ["MTRP_QA_LEGACY_FIXTURE_MODE"] = "1"


AUDIT_EVENT_COLUMNS = (
    "event_seq", "run_seq", "recorded_at", "event_type", "step_id", "route_step_id",
    "store", "operation", "status", "reason_code", "resource_count", "evidence_count",
    "skipped_count", "tool_call_count", "duration_ms", "receipt_hash", "response_sha256",
)

AUDIT_RUN_COLUMNS = (
    "run_seq", "started_at", "finished_at", "request_id", "route_id", "execution_id",
    "trace_id", "manifest_hash", "mode", "status", "reason_code", "next_step_id",
    "resource_count", "evidence_count", "step_count", "duration_ms",
)


def _synthetic_config(root: Path, default_config: Path) -> Path:
    """只生成测试配置；模板路径不会读取真实个人 Vault 或云标识。"""
    config = root / "config"
    shutil.copytree(default_config, config)
    vault = root / "Obsidian Vault"
    vault.mkdir()
    audit = config / ".mtrp" / "decision-log.sqlite3"
    audit.parent.mkdir()
    connection = sqlite3.connect(audit)
    try:
        connection.execute("CREATE TABLE stage2_execution_event (event_seq INTEGER PRIMARY KEY)")
        for column in AUDIT_EVENT_COLUMNS[1:]:
            connection.execute(f"ALTER TABLE stage2_execution_event ADD COLUMN {column}")
        for sequence in range(1, 8):
            values = [sequence, sequence, f"2026-01-01T00:00:0{sequence}+00:00", "qa", "qa", "qa",
                      "T", "read", "ok", "QA_FIXTURE", 0, 0, 0, 0, 0, "0" * 64, "0" * 64]
            placeholders = ",".join("?" for _ in values)
            connection.execute(
                f"INSERT INTO stage2_execution_event VALUES ({placeholders})", values,
            )
        connection.execute("CREATE TABLE stage2_execution_run (run_seq INTEGER PRIMARY KEY)")
        for column in AUDIT_RUN_COLUMNS[1:]:
            connection.execute(f"ALTER TABLE stage2_execution_run ADD COLUMN {column}")
        connection.execute(
            f"INSERT INTO stage2_execution_run VALUES ({','.join('?' for _ in AUDIT_RUN_COLUMNS)})",
            [1, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00",
             "qa-request", "qa-route", "qa-execution", "qa-trace", "0" * 64,
             "read-only", "COMPLETED", None, None, 0, 0, 0, 0],
        )
        connection.commit()
    finally:
        connection.close()
    for name in ("-wal", "-shm"):
        Path(str(audit) + name).write_bytes(b"")
    yaml = YAML()
    yaml.preserve_quotes = True
    stage2_path = config / "stage2.yaml"
    with stage2_path.open(encoding="utf-8") as stream:
        stage2 = yaml.load(stream)
    stage2["obsidian"]["vault_root"] = str(vault)
    stage2["lexiang_m"] = {
        "mode": "workbuddy-manifest",
        "team_id": "1" * 32,
        "space_id": "2" * 32,
        "root_entry_id": "3" * 32,
        "read_only": True,
        "timeout_seconds": 10,
    }
    with stage2_path.open("w", encoding="utf-8", newline="\n") as stream:
        yaml.dump(stage2, stream)
    return config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-dir", type=Path, required=True)
    args, pytest_args = parser.parse_known_args()
    test_dir = args.test_dir.resolve()
    sys.path.insert(0, str(test_dir))
    sys.path.insert(0, str(test_dir.parent))
    sys.modules.pop("conftest", None)
    import qa_support
    from panel.user_profiles import ProfileStore, UserPreferences

    with tempfile.TemporaryDirectory(prefix="mtrp-l01-full-suite-") as temporary:
        root = Path(temporary)
        users = root / "data" / "users.json"
        vault = root / "fixtures" / "Obsidian Vault"
        ProfileStore(users).ensure_default(UserPreferences(vault_root=str(vault), git_local_path=str(root)))
        qa_support.USERS_JSON = users
        qa_support.USERS_DIR = users.parent
        qa_support.EXPORTS_DIR = users.parent / "exports"
        config = _synthetic_config(root / "fixtures", test_dir.parent / "panel" / "default_config")
        qa_support.AUDIT_DB = config / ".mtrp" / "decision-log.sqlite3"
        qa_support.AUDIT_WAL = Path(str(qa_support.AUDIT_DB) + "-wal")
        qa_support.AUDIT_SHM = Path(str(qa_support.AUDIT_DB) + "-shm")
        from panel import review_api

        review_api._EXPORTS_DIR = qa_support.EXPORTS_DIR
        # 旧测试仍以 PANEL_DIR / "data/exports/..." 读取；仅在QA进程把仓库前缀映射到临时根。
        real_path_div = Path.__truediv__
        panel_data = real_path_div(test_dir.parent, "data")
        legacy_fixture_modules: set[str] = set()

        def isolated_path_div(path, relative):
            combined = real_path_div(path, relative)
            caller = sys._getframe(1).f_globals.get("__name__", "")
            if caller in legacy_fixture_modules and (combined == panel_data or panel_data in combined.parents):
                return users.parent / combined.relative_to(panel_data)
            return combined

        Path.__truediv__ = isolated_path_div
        real_popen = __import__("subprocess").Popen

        def isolated_popen(*popen_args, **popen_kwargs):
            """旧服务统一隔离；显式 --data-dir 用例保留自己的合成子进程环境。"""
            environment = dict(popen_kwargs.get("env") or os.environ)
            if os.environ.get("MTRP_QA_LEGACY_FIXTURE_MODE") == "1":
                environment["MTRP_PANEL_DATA_DIR"] = str(users.parent)
                environment["MTRP_PANEL_LEGACY_PROFILE"] = str(root / "no-legacy" / "users.json")
                environment["MTRP_PANEL_CONFIG_DIR"] = str(config)
            popen_kwargs["env"] = environment
            return real_popen(*popen_args, **popen_kwargs)

        import subprocess
        subprocess.Popen = isolated_popen
        original_start = qa_support.PanelServer.start

        def isolated_start(server, timeout=40.0):
            previous = os.environ.get("MTRP_PANEL_DATA_DIR")
            os.environ["MTRP_PANEL_DATA_DIR"] = str(users.parent)
            os.environ["MTRP_QA_LEGACY_FIXTURE_MODE"] = "1"
            try:
                return original_start(server, timeout)
            finally:
                os.environ.pop("MTRP_QA_LEGACY_FIXTURE_MODE", None)
                if previous is None:
                    os.environ.pop("MTRP_PANEL_DATA_DIR", None)
                else:
                    os.environ["MTRP_PANEL_DATA_DIR"] = previous

        qa_support.PanelServer.start = isolated_start
        os.environ["MTRP_PANEL_DATA_DIR"] = str(users.parent)
        original_collection = pytest.Session.perform_collect

        def isolated_collection(session, *collection_args, **collection_kwargs):
            os.environ.pop("MTRP_QA_LEGACY_FIXTURE_MODE", None)
            os.environ.pop("MTRP_PANEL_DATA_DIR", None)
            os.environ.pop("MTRP_PANEL_LEGACY_PROFILE", None)
            os.environ.pop("MTRP_PANEL_CONFIG_DIR", None)
            result = original_collection(session, *collection_args, **collection_kwargs)
            legacy_fixture_modules.update(
                module for module in sys.modules
                if module.startswith(("test_panel_api", "test_review_workbench"))
            )
            return result

        pytest.Session.perform_collect = isolated_collection
        return pytest.main(["-q", "--confcutdir", str(test_dir), str(test_dir), *pytest_args])


if __name__ == "__main__":
    raise SystemExit(main())
