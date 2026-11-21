"""MTR-P 四库只读面板的回环启动入口。"""

from __future__ import annotations

import argparse
import os
import socket
from collections.abc import Sequence

import uvicorn

from panel.settings import PanelError


LOOPBACK_HOST = "127.0.0.1"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析启动参数；端口 0 交给 OS 分配，不从 HTML 推断服务地址。"""

    parser = argparse.ArgumentParser(description="启动仅绑定 127.0.0.1 的 MTR-P 四库连接面板。")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，0 表示由系统自动分配，默认 8765。")
    parser.add_argument(
        "--data-dir",
        help="独立的本机绝对数据目录，保存用户台账与导出；绝不自动复制旧档案。",
    )
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开本机浏览器。")
    arguments = parser.parse_args(argv)
    if not 0 <= arguments.port <= 65535:
        parser.error("--port 必须在 0 到 65535 之间。")
    if arguments.data_dir is not None:
        try:
            from panel.runtime import resolve_runtime_paths

            resolve_runtime_paths(arguments.data_dir)
        except PanelError as exc:
            parser.error(exc.message)
    return arguments


def _open_browser_when_ready(url: str) -> None:
    """仅供测试替换浏览器动作；生产入口只在完成 bind 后调用。"""

    import webbrowser

    webbrowser.open(url, new=2, autoraise=True)


class ReadyServer(uvicorn.Server):
    """在 Uvicorn 完成 bind 后回报真实端口，再打开本机浏览器。"""

    def __init__(self, config: uvicorn.Config, *, open_browser: bool = False) -> None:
        super().__init__(config)
        self.open_browser = open_browser

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        if not self.started:
            return
        for listener in self.servers:
            for listener_socket in listener.sockets:
                address = listener_socket.getsockname()
                url = f"http://{LOOPBACK_HOST}:{address[1]}/"
                print(f"本地面板已就绪：{url}", flush=True)
                if self.open_browser:
                    _open_browser_when_ready(url)
                return


def main(argv: Sequence[str] | None = None) -> None:
    """设置当前进程的数据根，固定回环监听；不提供 0.0.0.0 覆盖项。"""

    arguments = parse_args(argv)
    if arguments.data_dir is not None:
        os.environ["MTRP_PANEL_DATA_DIR"] = arguments.data_dir
    config = uvicorn.Config(
        "panel.main:app",
        host=LOOPBACK_HOST,
        port=arguments.port,
        log_level="info",
        access_log=False,
    )
    server = ReadyServer(config, open_browser=not arguments.no_browser)
    server.run()


if __name__ == "__main__":
    main()
