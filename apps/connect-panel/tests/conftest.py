# -*- coding: utf-8 -*-
"""connect-panel QA 验收测试共享 fixture。

架构说明：
- 面板对 users.json 的路径是硬编码的（panel/main.py 指向 data/users.json），
  因此 QA 通过"字节级备份 + 套件结束恢复"的方式使用真实档案文件；
  多用户流测试必须走真实 API 与真实写盘路径（原子写、进程内锁）。
- config/、审计库、fixture vault 全程只读；套件启动前先做基线快照
  （users.json 原始字节、审计三文件 hash、主仓聚合 hash），供末尾的不变性
  断言使用。
- fixture server 为 session 级子进程 uvicorn（127.0.0.1，9678–9681 探测端口），
  teardown 顺序：杀进程 → 等待端口释放 → 恢复 users.json → 清理临时残留。
  不引入任何新 pip 依赖，客户端使用标准库 urllib。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from qa_support import (
    AUDIT_DB,
    AUDIT_SHM,
    AUDIT_WAL,
    EXPORTS_DIR,
    PANEL_VENV_PYTHON,
    USERS_DIR,
    USERS_JSON,
    PanelServer,
    aggregation_digest,
    aggregation_paths,
    file_sha256,
    pick_free_port,
)


@pytest.fixture(scope="session")
def server(tmp_path_factory: pytest.TempPathFactory):
    """session 级启动一次真实 uvicorn 面板并捕获不动点基线。"""

    if not PANEL_VENV_PYTHON.is_file():
        pytest.fail("QA 找不到 .venv/Scripts/python.exe，请在主仓根目录运行。")
    users_backup: Path = tmp_path_factory.mktemp("qa-users-backup") / "users.json"
    original_bytes = USERS_JSON.read_bytes()
    users_backup.write_bytes(original_bytes)
    exports_before = {path.name for path in EXPORTS_DIR.glob("*")} if EXPORTS_DIR.is_dir() else set()

    harness = PanelServer(pick_free_port())
    harness.baseline = {
        "users_original_bytes": original_bytes,
        "audit_main_sha": file_sha256(AUDIT_DB),
        "audit_wal_sha": file_sha256(AUDIT_WAL),
        "audit_wal_size": AUDIT_WAL.stat().st_size,
        "audit_shm_sha": file_sha256(AUDIT_SHM),
        "aggregation_paths_count": len(aggregation_paths()),
        "aggregation_digest": aggregation_digest(aggregation_paths()),
    }
    harness.start()
    try:
        yield harness
    finally:
        harness.stop()
        # 恢复 users.json 原始字节（含冒烟遗留用户，QA 不删任何既有档案）。
        USERS_JSON.write_bytes(original_bytes)
        for stray in USERS_DIR.glob(".users-*.tmp"):
            try:
                stray.unlink()
            except OSError:
                pass
        if EXPORTS_DIR.is_dir():
            for stray in EXPORTS_DIR.glob("*"):
                if stray.name not in exports_before and stray.is_file():
                    try:
                        stray.unlink()
                    except OSError:
                        pass
        shutil.rmtree(USERS_DIR / "qa-escape-scratch", ignore_errors=True)
        restored = USERS_JSON.read_bytes()
        assert restored == original_bytes, "teardown 未能恢复 users.json 原始字节"
