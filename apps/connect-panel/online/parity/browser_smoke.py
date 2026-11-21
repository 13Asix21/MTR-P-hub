# -*- coding: utf-8 -*-
"""O01 真实 Chrome 验收脚本；使用测试目录，不安装浏览器、不接触个人 Vault。"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import functools
import hashlib
import json
import mimetypes
import subprocess
import sys
import tempfile
import threading
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import expect, sync_playwright

DEFAULT_OVERALL_TIMEOUT_SECONDS = 180.0
DEFAULT_PHASE_TIMEOUT_SECONDS = 30.0
BROWSER_LAUNCH_TIMEOUT_MS = 15000
BROWSER_ACTION_TIMEOUT_MS = 12000
BROWSER_NAVIGATION_TIMEOUT_MS = 20000


def _phase_timeout(timeout_seconds: float) -> int:
    """阶段等待只能显式请求缩短，不能超过 CLI 的阶段上限。"""

    if timeout_seconds <= 0:
        raise ValueError("O01 阶段超时必须是正数。")
    return int(timeout_seconds * 1000)


def run(
    root: Path,
    channel: str = "chrome",
    *,
    overall_timeout_seconds: float = DEFAULT_OVERALL_TIMEOUT_SECONDS,
    phase_timeout_seconds: float = DEFAULT_PHASE_TIMEOUT_SECONDS,
) -> dict[str, object]:
    mimetypes.add_type("text/javascript", ".mjs")
    online = root / "apps" / "connect-panel" / "online"
    fixture = online / "parity" / "fixtures" / "vault"
    if overall_timeout_seconds <= 0 or phase_timeout_seconds <= 0:
        raise ValueError("O01 整体与阶段超时必须是正数。")
    results: dict[str, object] = {"browser": channel, "assertions": []}

    class QuietStaticHandler(SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            """减少测试日志噪声；测试失败仍由断言输出。"""

    handler = functools.partial(QuietStaticHandler, directory=str(online))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory(prefix="mtrp-o01-browser-"), contextlib.ExitStack() as stack:
        stack.callback(thread.join, 3)
        stack.callback(server.server_close)
        stack.callback(server.shutdown)
        with sync_playwright() as playwright, contextlib.ExitStack() as browser_stack:
            print("STEP 启动 Chrome", flush=True)
            browser = playwright.chromium.launch(channel=channel, headless=True, timeout=BROWSER_LAUNCH_TIMEOUT_MS)
            browser_stack.callback(browser.close)
            context = browser.new_context(accept_downloads=True)
            browser_stack.callback(context.close)
            context.set_default_timeout(min(BROWSER_ACTION_TIMEOUT_MS, _phase_timeout(phase_timeout_seconds)))
            context.set_default_navigation_timeout(min(BROWSER_NAVIGATION_TIMEOUT_MS, _phase_timeout(phase_timeout_seconds)))
            page = context.new_page()
            page.on("requestfailed", lambda request: print("REQUEST FAILED:", request.url, request.failure, flush=True))
            class AssertionLog(list[str]):
                """就地记录并输出真实序号，最终 JSON 与终端使用同一列表。"""

                def append(self, message: str) -> None:
                    super().append(message)
                    print(f"STEP {len(self)} {message}", flush=True)

            results["assertions"] = AssertionLog()
            print("STEP 打开静态页面", flush=True)
            page_errors: list[str] = []
            page.on("pageerror", lambda error: (page_errors.append(str(error)), print("PAGE ERROR:", error, flush=True)))
            page.on("console", lambda message: print("BROWSER:", message.type, message.text, flush=True) if message.type == "error" else None)
            # 仅测试注入真实 FileSystemDirectoryHandle 形状，不宣称系统目录选择器已自动化。
            page.add_init_script(
                """
                globalThis.showDirectoryPicker = async ({mode}) => {
                  if (mode !== 'read') throw new Error('non-read mode');
                  const vault = await navigator.storage.getDirectory();
                  const root = await vault.getDirectoryHandle('test-vault', {create: true});
                  const managed = await root.getDirectoryHandle('2-Permanent', {create: true});
                  const a = await managed.getFileHandle('a.md', {create: true});
                  let writer = await a.createWritable();
                  await writer.write('---\\ntags: [mtr/trial]\\n---\\n# A\\n正文'); await writer.close();
                  const invalid = await managed.getFileHandle('invalid.md', {create: true});
                  writer = await invalid.createWritable();
                  await writer.write('---\\ntags: [\\n---\\n坏格式原文'); await writer.close();
                  return root;
                };
                globalThis.confirmMessages = [];
                globalThis.confirm = (message) => { globalThis.confirmMessages.push(message); return true; };
                """
            )
            base = f"http://127.0.0.1:{server.server_port}/"
            page.goto(base, wait_until="networkidle")
            expect(page.get_by_text("身份：本地浏览器档案（不是账号登录）", exact=False)).to_be_visible()
            expect(page.get_by_text("仅保存于当前浏览器", exact=False)).to_be_visible()
            page.locator("#select-vault").click()
            expect(page.locator("#review-detail")).to_be_visible(timeout=_phase_timeout(phase_timeout_seconds))
            expect(page.locator("tbody tr")).to_have_count(2)
            expect(page.locator("#note-list")).to_contain_text("INVALID_YAML")
            page.get_by_role("combobox", name="按审核状态筛选").select_option("invalid")
            expect(page.locator("tbody tr")).to_have_count(1)
            expect(page.locator("tbody tr")).to_contain_text("invalid.md")
            page.get_by_role("combobox", name="按审核状态筛选").select_option("all")
            page.get_by_role("searchbox", name="搜索相对路径").fill("不存在")
            expect(page.locator("#note-list")).to_contain_text("没有符合当前条件")
            page.get_by_role("searchbox", name="搜索相对路径").fill("")
            expect(page.locator("tbody tr")).to_have_count(2)
            results["assertions"].append("真实 Chrome UI 扫描、列表、筛选和 INVALID_YAML 分母")
            page.get_by_role("button", name="2-Permanent/a.md", exact=True).click()
            expect(page.locator("#note-source")).to_contain_text("正文")
            page.locator("#review-reason").fill("浏览器闭环理由")
            page.locator('[data-action="approved"]').click()
            expect(page.locator(".summary-panel")).to_contain_text("已审 1 / 2")
            page.locator("#review-reason").fill("撤销前理由")
            page.locator('[data-action="undo"]').click()
            expect(page.locator(".summary-panel")).to_contain_text("已审 0 / 2")
            page.locator('[data-action="rejected"]').click()
            expect(page.locator(".summary-panel")).to_contain_text("拒绝 1")
            page.locator('[data-action="skipped"]').click()
            expect(page.locator(".summary-panel")).to_contain_text("跳过 1")
            page.locator('[data-action="approved"]').click()
            expect(page.locator(".summary-panel")).to_contain_text("通过 1")
            results["assertions"].append("详情、理由、通过/拒绝/跳过/撤销与进度")
            page.evaluate("""async () => {
              const root = await (await navigator.storage.getDirectory()).getDirectoryHandle('test-vault');
              const file = await (await root.getDirectoryHandle('2-Permanent')).getFileHandle('a.md');
              const writer = await file.createWritable();
              await writer.write('---\\ntags: [mtr/trial]\\n---\\n外部改动');
              await writer.close();
            }""")
            page.locator('[data-action="undo"]').click()
            expect(page.locator("#app-notice")).to_contain_text("当前文件与详情 SHA 不一致", timeout=_phase_timeout(phase_timeout_seconds))
            expect(page.locator(".summary-panel")).to_contain_text("已审 1 / 2")
            page.evaluate("""async () => {
              const root = await (await navigator.storage.getDirectory()).getDirectoryHandle('test-vault');
              const file = await (await root.getDirectoryHandle('2-Permanent')).getFileHandle('a.md');
              const writer = await file.createWritable();
              await writer.write('---\\ntags: [mtr/trial]\\n---\\n# A\\n正文');
              await writer.close();
            }""")
            page.get_by_role("button", name="2-Permanent/a.md", exact=True).click()
            expect(page.locator('[data-action="undo"]')).to_be_enabled()
            results["assertions"].append("外部内容变化使撤销 CONTENT_CHANGED，保留已读队列")
            page.locator("body").click(position={"x": 1, "y": 1})
            page.keyboard.press("j")
            expect(page.locator("#review-detail h2")).to_have_text("2-Permanent/invalid.md")
            expect(page.locator("#note-source")).to_contain_text("tags: [")
            page.keyboard.press("a")
            expect(page.locator(".summary-panel")).to_contain_text("通过 2")
            expect(page.locator("#app-notice")).to_contain_text("2-Permanent/invalid.md · approved")
            page.keyboard.press("k")
            expect(page.locator("#review-detail h2")).to_have_text("2-Permanent/a.md")
            page.keyboard.press("r")
            expect(page.locator(".summary-panel")).to_contain_text("拒绝 1")
            page.keyboard.press("s")
            expect(page.locator(".summary-panel")).to_contain_text("跳过 1")
            page.keyboard.press("u")
            expect(page.locator(".summary-panel")).to_contain_text("已审 1 / 2")
            page.keyboard.press("a")
            expect(page.locator(".summary-panel")).to_contain_text("通过 2")
            page.keyboard.press("u")
            expect(page.locator(".summary-panel")).to_contain_text("已审 1 / 2")
            expect(page.locator(".summary-panel")).to_contain_text("通过 1")
            expect(page.locator("#app-notice")).to_contain_text("2-Permanent/a.md · undo")
            results["assertions"].append("J/K/A/R/S/U 实际改变详情选择和审核进度，INVALID_YAML 原文可审核")
            page.get_by_role("checkbox", name="选择当前筛选全部笔记").check()
            page.locator('[data-batch-action="approved"]').click()
            expect(page.locator("#app-notice")).to_contain_text("批量完成 2 篇")
            expect(page.locator(".summary-panel")).to_contain_text("通过 2")
            page.get_by_role("checkbox", name="选择当前筛选全部笔记").check()
            page.locator('[data-batch-action="undo"]').click()
            expect(page.locator("#app-notice")).to_contain_text("批量完成 2 篇")
            expect(page.locator(".summary-panel")).to_contain_text("已审 0 / 2")
            page.get_by_role("button", name="2-Permanent/a.md", exact=True).click()
            expect(page.locator('[data-action="approved"]')).to_be_visible()
            page.locator('[data-action="approved"]').click()
            expect(page.locator(".summary-panel")).to_contain_text("通过 1")
            results["assertions"].append("真实 UI 两篇批量通过、批量撤销和导出前重审")
            downloads = []
            page.on("download", lambda download: downloads.append(download))
            page.locator("#export-approved").click()
            expect(page.locator("#app-notice")).to_contain_text("已请求浏览器下载")
            assert len(downloads) == 2
            json_download, csv_download = downloads
            exported = json.loads(Path(json_download.path()).read_text(encoding="utf-8"))
            assert len(exported["items"]) == 1
            assert exported["items"][0]["action"] == "approved"
            csv_bytes = Path(csv_download.path()).read_bytes()
            assert csv_bytes.startswith(b"\xef\xbb\xbf") and b"2-Permanent/a.md" in csv_bytes
            results["assertions"].append("dry-run 后读取真实 JSON+CSV 浏览器下载内容")
            page.evaluate("async () => { await navigator.serviceWorker.ready; }")
            page.reload(wait_until="networkidle")
            expect(page.locator("#restore-vault")).to_be_visible()
            page.locator("#restore-vault").click()
            expect(page.locator(".summary-panel")).to_contain_text("已审 1 / 2", timeout=_phase_timeout(phase_timeout_seconds))
            results["assertions"].append("页面刷新、只读授权恢复及 IndexedDB 审核恢复")
            registrations = page.evaluate("async () => (await navigator.serviceWorker.getRegistrations()).length")
            assert registrations == 1, f"Service Worker 注册数 {registrations}"
            cache_state = page.evaluate("async () => ({shell: (await caches.keys()).some(k => k.startsWith('mtrp-shell-')), idb: await new Promise((resolve, reject) => { const r = indexedDB.open('mtrp-online-workbench'); r.onsuccess = () => { const db = r.result; const tx = db.transaction('pending_actions'); const q = tx.objectStore('pending_actions').getAll(); q.onsuccess = () => resolve(q.result.length); q.onerror = () => reject(q.error); }; r.onerror = () => reject(r.error); })})")
            assert cache_state["shell"] is True
            assert cache_state["idb"] > 0
            context.set_offline(True)
            page.reload(wait_until="domcontentloaded")
            expect(page.locator("#restore-vault")).to_be_visible(timeout=_phase_timeout(phase_timeout_seconds))
            page.locator("#restore-vault").click()
            expect(page.locator(".summary-panel")).to_contain_text("已审 1 / 2", timeout=_phase_timeout(phase_timeout_seconds))
            page.get_by_role("button", name="2-Permanent/invalid.md", exact=True).click()
            expect(page.locator("#note-source")).to_contain_text("坏格式原文")
            page.locator('[data-action="approved"]').click()
            expect(page.locator(".summary-panel")).to_contain_text("通过 2")
            expect(page.locator(".privacy-panel")).to_contain_text("浏览器离线")
            assert page.evaluate("navigator.onLine") is False
            results["assertions"].append("断网刷新应用壳、恢复目录并实际提交审核到 IndexedDB")
            context.set_offline(False)
            idb_before = page.evaluate("async () => await new Promise((resolve, reject) => { const r = indexedDB.open('mtrp-online-workbench'); r.onsuccess = () => { const q = r.result.transaction('pending_actions').objectStore('pending_actions').getAll(); q.onsuccess = () => resolve(q.result.length); q.onerror = () => reject(q.error); }; r.onerror = () => reject(r.error); })")
            page.evaluate("async () => { const registration = await navigator.serviceWorker.getRegistration(); await registration.update(); }")
            idb_after = page.evaluate("async () => await new Promise((resolve, reject) => { const r = indexedDB.open('mtrp-online-workbench'); r.onsuccess = () => { const q = r.result.transaction('pending_actions').objectStore('pending_actions').getAll(); q.onsuccess = () => resolve(q.result.length); q.onerror = () => reject(q.error); }; r.onerror = () => reject(r.error); })")
            assert idb_after == idb_before
            results["assertions"].append("Service Worker update 不清理 IndexedDB")
            page.evaluate("globalThis.confirm = () => false")
            page.locator("#clear-local-data").click()
            expect(page.locator(".summary-panel")).to_contain_text("已审 2 / 2")
            page.evaluate("""() => {
              globalThis.confirmMessages = [];
              globalThis.confirm = (message) => { globalThis.confirmMessages.push(message); return true; };
            }""")
            page.locator("#clear-local-data").click()
            expect(page.locator(".summary-panel")).to_contain_text("已审 0 / 2")
            assert page.evaluate("globalThis.confirmMessages.length") == 2
            assert page.evaluate("async () => await new Promise((resolve, reject) => { const r = indexedDB.open('mtrp-online-workbench'); r.onsuccess = () => { const q = r.result.transaction('pending_actions').objectStore('pending_actions').getAll(); q.onsuccess = () => resolve(q.result.length); q.onerror = () => reject(q.error); }; r.onerror = () => reject(r.error); })") == 0
            fixture_text = page.evaluate("""async () => {
              const root = await (await navigator.storage.getDirectory()).getDirectoryHandle('test-vault');
              return await (await (await (await root.getDirectoryHandle('2-Permanent')).getFileHandle('a.md')).getFile()).text();
            }""")
            assert fixture_text == '---\ntags: [mtr/trial]\n---\n# A\n正文'
            results["assertions"].append("首次拒绝确认不清台账，双确认才清除；OPFS 测试文件字节未改")
            broken_page = context.new_page()
            broken_page.add_init_script("""
              globalThis.showDirectoryPicker = async () => { throw new Error('should not pick'); };
              globalThis.testIndexedDb = { open() {
                const request = {};
                queueMicrotask(() => {
                  request.error = new DOMException('blocked by test', 'SecurityError');
                  request.onerror?.();
                });
                return request;
              } };
              Object.defineProperty(globalThis, 'indexedDB', { configurable: true, get: () => globalThis.testIndexedDb });
            """)
            broken_page.goto(base, wait_until="networkidle")
            expect(broken_page.locator("#storage-warning")).to_contain_text("隐私模式")
            broken_page.locator("#select-vault").click()
            expect(broken_page.locator("#app-notice")).to_contain_text("本机离线台账不可用")
            results["assertions"].append("真实 Chrome IndexedDB 拒绝时显示隐私/存储边界且停止审核")
            broken_page.close()
            assert not page_errors, page_errors
            results["page_errors"] = page_errors
            results["service_worker_registrations"] = registrations
            # 正常退出也走与失败路径相同的 LIFO 清理，避免重复关闭掩盖首个异常。
            assert len(results["assertions"]) == 11, "O01 必须完整执行并记录 11 组断言。"
    results["fixture_sha256"] = hashlib.sha256((fixture / "2-Permanent" / "valid" / "13-no-frontmatter.md").read_bytes()).hexdigest()
    return results


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """在 Windows 用独立 Job 绑定测试进程树，防止超时留下 Chrome 后代。"""

    if sys.platform != "win32":
        process.kill()
        return
    process_access = 0x0400 | 0x1000 | 0x0001  # QUERY_INFORMATION | SET_QUOTA | TERMINATE
    handle = ctypes.windll.kernel32.OpenProcess(process_access, False, process.pid)
    if not handle:
        process.kill()
        return
    job = None
    try:
        job = ctypes.windll.kernel32.CreateJobObjectW(None, None)
        if job and ctypes.windll.kernel32.AssignProcessToJobObject(job, handle):
            # Windows 8+ 支持嵌套 Job；失败时明确回退，不宣称已清理后代。
            if ctypes.windll.kernel32.TerminateJobObject(job, 1):
                return
        process.kill()
    finally:
        if job:
            ctypes.windll.kernel32.CloseHandle(job)
        ctypes.windll.kernel32.CloseHandle(handle)


def main(argv: list[str] | None = None) -> None:
    """隔离浏览器进程并提供父进程整体超时，失败不以通过退出。"""

    global expect, sync_playwright
    if "--child" in (sys.argv[1:] if argv is None else argv):
        from playwright.sync_api import expect, sync_playwright
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[4])
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--overall-timeout", type=float, default=DEFAULT_OVERALL_TIMEOUT_SECONDS, help="整体秒数，默认 180。")
    parser.add_argument("--phase-timeout", type=float, default=DEFAULT_PHASE_TIMEOUT_SECONDS, help="阶段等待秒数上限，默认 30。")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    if arguments.overall_timeout <= 0 or arguments.phase_timeout <= 0:
        parser.error("整体与阶段超时必须是正数。")
    if arguments.child:
        try:
            results = run(
                arguments.root.resolve(),
                arguments.channel,
                overall_timeout_seconds=arguments.overall_timeout,
                phase_timeout_seconds=arguments.phase_timeout,
            )
        except BaseException:
            print("O01 SMOKE FAILED", flush=True)
            traceback.print_exc()
            raise SystemExit(1) from None
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    # 父进程 watchdog 可对卡死的 Playwright 驱动强制回收；普通失败由子进程 finally 正常清理。
    # 子进程先检查测试依赖，顶层不再无条件导入 Playwright。
    command = [
        sys.executable,
        "-c",
        "import playwright.sync_api",
    ]
    dependency_check = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False)
    if dependency_check.returncode != 0:
        print("O01 SMOKE FAILED：当前解释器缺少 Playwright；请使用已配置的测试环境，不自动安装浏览器。", flush=True)
        print(dependency_check.stderr, end="", flush=True)
        raise SystemExit(1)
    command = [
        sys.executable,
        str(Path(__file__).with_name("browser_smoke_child.py").resolve()),
        "--root", str(arguments.root.resolve()),
        "--channel", arguments.channel,
        "--overall-timeout", str(arguments.overall_timeout),
        "--phase-timeout", str(arguments.phase_timeout),
        "--child",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    output = ""
    try:
        output, _ = process.communicate(timeout=arguments.overall_timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process)
        output, _ = process.communicate(timeout=10)
        print(output, end="", flush=True)
        print(
            f"O01 SMOKE FAILED：整体超时 {arguments.overall_timeout:g}s；"
            "已终止测试子进程，请查看最后一个 STEP。",
            flush=True,
        )
        raise SystemExit(1) from None
    print(output, end="", flush=True)
    if process.returncode != 0:
        raise SystemExit(process.returncode)
    return None


if __name__ == "__main__":
    main()
