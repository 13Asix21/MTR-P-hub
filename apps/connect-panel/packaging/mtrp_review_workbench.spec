# PyInstaller 本地完整版构建配置；不打包真实 config、data、SQLite、.venv 或 .workbuddy。
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

APP = Path(SPEC).resolve().parents[1]
REPO = APP.parents[1]

analysis = Analysis(
    [str(APP / "packaging" / "launcher.py")],
    pathex=[str(APP), str(REPO / "src")],
    binaries=[],
    datas=[
        (str(APP / "panel"), "panel"),
        (str(APP / "static"), "static"),
        (str(APP / "run_panel.py"), "."),
        (str(REPO / "src" / "mtrp_hub"), "mtrp_src/mtrp_hub"),
    ],
    hiddenimports=collect_submodules("fastapi") + collect_submodules("uvicorn") + collect_submodules("panel") + [
        # panel 作为源码数据随包分发，main 又由 Uvicorn 按字符串延迟导入，
        # 必须收集其完整依赖图，不能只靠 launcher 的静态 import 猜测运行时模块。
        "sqlite3",
        "_sqlite3",
        "mtrp_hub.adapters.obsidian_markdown",
        "mtrp_hub.canonical",
        "mtrp_hub.domain.enums",
        "mtrp_hub.domain.models",
        "ruamel.yaml",
        "starlette",
        "starlette.exceptions",
        "starlette.middleware",
        "starlette.requests",
        "starlette.responses",
        "starlette.routing",
        "starlette.staticfiles",
        "anyio",
        "anyio.abc",
        "anyio.streams.memory",
        "sniffio",
        "click",
        "h11",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest", "pytest_asyncio", "playwright", "PIL", "tkinter",
        "mtrp_hub.cli", "mtrp_hub.approval", "mtrp_hub.lifecycle",
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="MTRP-ReviewWorkbench",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MTRP-ReviewWorkbench",
)
