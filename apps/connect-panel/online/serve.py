#!/usr/bin/env python3
"""Serve the static workbench on loopback with browser-safe module MIME types."""

from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ONLINE_ROOT = Path(__file__).resolve().parent


class WorkbenchStaticHandler(SimpleHTTPRequestHandler):
    """Keep the application self-contained while fixing platform MIME defaults."""

    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".css": "text/css",
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".webmanifest": "application/manifest+json",
    }

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()


def create_server(port: int = 8080, root: Path = ONLINE_ROOT) -> ThreadingHTTPServer:
    handler = partial(WorkbenchStaticHandler, directory=str(root))
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the MTR-P browser workbench on loopback only.")
    parser.add_argument("--port", type=int, default=8080, help="TCP port on 127.0.0.1; 0 selects a free port")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")

    server = create_server(args.port)
    print(f"审核工作台已就绪：http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
