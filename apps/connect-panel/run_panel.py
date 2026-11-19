"""MTR-P 四库只读面板启动入口。"""

from __future__ import annotations

import argparse

import uvicorn

from panel.main import app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动仅绑定 127.0.0.1 的 MTR-P 四库连接面板。")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765。")
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65535:
        parser.error("--port 必须在 1 到 65535 之间。")
    return arguments


def main() -> None:
    """以固定回环地址启动服务；不提供 0.0.0.0 覆盖项。"""

    arguments = parse_args()
    uvicorn.run(app, host="127.0.0.1", port=arguments.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
