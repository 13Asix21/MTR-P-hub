"""保证代理内置的路由副本与主仓实现一致，防止两边各自漂移。

代理部署包只上传 `apps/mtrp-proxy/`，所以它必须自带一份路由逻辑；
但同一套规则不允许出现两份不同实现——本测试用内容比对把两者钉死。
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL = REPO_ROOT / "src" / "mtrp_hub" / "routing"
MIRROR = REPO_ROOT / "apps" / "mtrp-proxy" / "_routing"

SHARED_MODULES = ("references.py", "serial.py", "host.py", "mcp_reader.py")


@pytest.mark.parametrize("module", SHARED_MODULES)
def test_mirror_matches_canonical(module: str) -> None:
    canonical = (CANONICAL / module).read_text(encoding="utf-8")
    mirror = (MIRROR / module).read_text(encoding="utf-8")
    # 副本允许把包导入改成相对导入，其余内容必须逐字一致。
    normalized = mirror.replace("from .references import", "from mtrp_hub.routing.references import")
    normalized = normalized.replace("from .host import", "from mtrp_hub.routing.host import")
    normalized = normalized.replace("from .mcp_reader import", "from mtrp_hub.routing.mcp_reader import")
    normalized = normalized.replace("from .serial import", "from mtrp_hub.routing.serial import")
    assert normalized == canonical, f"{module} 与主仓实现不一致，请同步后再发布"


def test_mirror_package_is_importable_standalone() -> None:
    """代理侧必须能独立导入，不依赖仓库 src。"""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "_mirror_probe_references", MIRROR / "references.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["_mirror_probe_references"] = module
    spec.loader.exec_module(module)
    refs = module.extract_references("见 o/r#docs/a.md")
    assert refs and refs[0].path == "docs/a.md"
    sys.modules.pop("_mirror_probe_references", None)
