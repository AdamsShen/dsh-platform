"""开发态启动入口：`uv run python scripts/serve.py`（等价于 make run）。"""

from __future__ import annotations

import uvicorn

from ao.settings import settings


def main() -> None:
    uvicorn.run("ao.web:app", host="127.0.0.1", port=8000, reload=settings.env == "dev")


if __name__ == "__main__":
    main()
