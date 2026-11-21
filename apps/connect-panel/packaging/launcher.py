"""Windows 本地完整版入口；不依赖开发仓 cwd、系统 Python 或主仓配置。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    # Windows 控制台默认 GBK，显式使用 UTF-8，避免启动说明与错误乱码。
    os.environ.setdefault("PYTHONUTF8", "1")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    os.environ["MTRP_PANEL_PACKAGED"] = "1"
    if getattr(sys, "frozen", False):
        application_root = Path(sys.executable).resolve().parent
        bundled_source = Path(getattr(sys, "_MEIPASS", application_root)) / "mtrp_src"
        os.environ["MTRP_PANEL_LEGACY_PROFILE"] = str(
            application_root / "data" / "users.json"
        )
        sys.path.insert(0, str(bundled_source))
    from panel.runtime import application_directory

    sys.path.insert(0, str(application_directory()))
    from run_panel import main as run

    run()


if __name__ == "__main__":
    main()
