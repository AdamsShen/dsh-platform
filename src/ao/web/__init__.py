"""Web 控制面入口：`uvicorn ao.web:app`。"""

from ao.web.app import app, create_app

__all__ = ["app", "create_app"]
