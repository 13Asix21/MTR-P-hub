"""O01 浏览器子进程入口；顶层导入 Playwright 仅发生在依赖预检后。"""

import sys

from browser_smoke import main


if __name__ == "__main__":
    main(["--child", *sys.argv[1:]])
