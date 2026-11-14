"""顺序执行 migrations/*.sql。

已应用的版本记在 `schema_migrations`，不回改历史迁移文件；需要变更就新增一个文件。
"""

from __future__ import annotations

import sys

from ao.db import close_pool, get_pool
from ao.settings import PROJECT_ROOT

MIGRATIONS_DIR = PROJECT_ROOT / "migrations"

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def applied_versions() -> set[str]:
    with get_pool().connection() as conn:
        conn.autocommit = True
        conn.execute(_BOOTSTRAP)
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row["version"] for row in rows}


def migrate() -> list[str]:
    done = applied_versions()
    applied: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in done:
            continue
        sql = path.read_text(encoding="utf-8")
        with get_pool().connection() as conn:
            conn.autocommit = False
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,)
                )
        applied.append(path.name)
    return applied


def main() -> int:
    try:
        names = migrate()
    finally:
        close_pool()
    if names:
        print("已应用迁移: " + ", ".join(names))
    else:
        print("没有待应用的迁移")
    return 0


if __name__ == "__main__":
    sys.exit(main())
