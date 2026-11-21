"""对真实打包 EXE 做独立数据根、只读 Vault、审核和导出闭环验收。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


def request(port: int, method: str, route: str, payload: dict | None = None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{route}", method=method, data=body,
        headers={"Content-Type": "application/json"} if body else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--keep", type=Path)
    args = parser.parse_args()
    executable = args.executable.resolve()
    if not executable.is_file():
        raise SystemExit("找不到待验收的 EXE。")
    with tempfile.TemporaryDirectory(prefix="mtrp-exe-acceptance-") as raw:
        root = Path(raw)
        work = root / "中文 空格"
        data = work / "独立数据"
        vault = work / "测试 Vault"
        managed = vault / "2-Permanent"
        managed.mkdir(parents=True)
        note = managed / "审核笔记.md"
        note.write_text("---\ntags: [mtr/trial]\n---\r\n# 测试\r\n正文", encoding="utf-8")
        (vault / ".obsidian").mkdir()
        original = hashlib.sha256(note.read_bytes()).hexdigest()
        if args.keep:
            args.keep.mkdir(parents=True, exist_ok=True)
            copied = args.keep / executable.parent.name
            if copied.exists():
                raise SystemExit("保留目录已存在，拒绝覆盖。")
            shutil.copytree(executable.parent, copied)
            executable = copied / executable.name
        environment = dict(os.environ)
        for key in ("MTRP_PANEL_CONFIG_DIR", "MTRP_PANEL_DATA_DIR", "MTRP_PANEL_LEGACY_PROFILE", "PYTHONPATH"):
            environment.pop(key, None)
        environment["PYTHONUTF8"] = "1"
        environment["MTRP_PANEL_LEGACY_PROFILE"] = str(work / "无旧档案" / "users.json")
        process = subprocess.Popen(
            [str(executable), "--port", "0", "--data-dir", str(data), "--no-browser"],
            cwd=work, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        try:
            deadline = time.monotonic() + 90
            port = None
            while time.monotonic() < deadline:
                line = process.stdout.readline()
                if line:
                    print(line.rstrip(), flush=True)
                    matched = re.search(r"http://127\.0\.0\.1:(\d+)/", line)
                    if matched:
                        port = int(matched.group(1))
                        break
                elif process.poll() is not None:
                    raise RuntimeError("EXE 启动提前退出。")
            if port is None:
                raise RuntimeError("90 秒内未获得回环监听地址。")
            status, health = request(port, "GET", "/api/health")
            assert status == 200 and health["data"]["ready"] is True
            assert health["data"]["migration_required"] is False
            status, _ = request(port, "POST", "/api/check/vault", {"path": str(vault)})
            assert status == 200
            status, listing = request(port, "POST", "/api/review/scan", {"path": str(vault)})
            assert status == 200, listing
            item = listing["data"]["items"][0]
            assert item["mtr_state"] == "trial", item
            assert item["sha256"] == original
            action = {
                "path": item["vault_relative_path"], "content_sha256": original,
                "action": "approved", "reason": "打包闭环测试",
            }
            status, decided = request(port, "POST", "/api/review/decide", action)
            assert status == 200, decided
            status, exported = request(port, "POST", "/api/review/export", {"include": "approved"})
            assert status == 200, exported
            files = exported["data"].get("files_absolute", {})
            json_path = Path(files.get("json", data / "exports" / Path(exported["data"]["files"]["json"]).name))
            assert json_path.is_file(), exported
            document = json.loads(json_path.read_text(encoding="utf-8"))
            assert document["items"][0]["vault_relative_path"] == "2-Permanent/审核笔记.md"
            assert hashlib.sha256(note.read_bytes()).hexdigest() == original
            assert json_path.parent == (data / "exports").resolve()
            assert not (executable.parent / "data" / "users.json").exists()
            print(json.dumps({
                "status": "passed", "health": status, "scanned": listing["data"]["total"],
                "mtr_state": item["mtr_state"], "exported": document["counts"]["exported_items"],
                "vault_sha256_unchanged": True, "independent_data_dir": True,
            }, ensure_ascii=False, indent=2), flush=True)
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            if process.stdout:
                remainder = process.stdout.read()
                if remainder:
                    print(remainder, flush=True)
                process.stdout.close()


if __name__ == "__main__":
    main()
