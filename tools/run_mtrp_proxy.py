"""Start the MTR-P lexiang proxy with credentials loaded from the user's .env."""

import os
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

ENV_PATH = Path(r"E:\ASoftwareManage\Agents\AI本体和知识库的结合\agent-bridge\.env")
env = dotenv_values(ENV_PATH)
token = env.get("AGENT_BRIDGE_LEXIANG__TOKEN") or env.get("AGENT_BRIDGE_LEXIANG_TOKEN") or ""
if not token:
    sys.exit("缺少乐享 token，未启动代理。")

os.environ["MTRP_LEXIANG_TOKEN"] = token
os.environ.setdefault("MTRP_LEXIANG_SPACE_ID", "c3f157b3d03d4c7fa46a8fb8e70b1ae7")
os.environ.setdefault(
    "MTRP_ALLOWED_ORIGINS",
    "https://mtrp-review-workbench.app.workbuddy.host,http://127.0.0.1:8766,http://localhost:8766",
)

sys.exit(subprocess.call([
    sys.executable,
    str(Path(__file__).with_name("mtrp_proxy.py")),
    "--port", "8012",
]))
