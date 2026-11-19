"""造测试主体目录（P5-T01）。

用法：
    uv run python scripts/seed_users.py            # 默认租户，10 个主体
    uv run python scripts/seed_users.py 100         # 100 个主体（压扇出用）
    uv run python scripts/seed_users.py 20 t_demo   # 指定租户

幂等：按 (tenant, external_ref) 更新，重复执行不会堆记录。
"""

from __future__ import annotations

import sys

from ao import users as users_store
from ao.db import close_pool, tx
from ao.settings import settings


def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    tenant = sys.argv[2] if len(sys.argv) > 2 else settings.default_tenant
    with tx() as conn:
        conn.execute(
            "INSERT INTO tenants (id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (tenant, tenant),
        )
    for index in range(count):
        users_store.upsert(
            tenant_id=tenant,
            external_ref=f"user-{index:03d}",
            display_name=f"测试用户{index:03d}",
        )
    print(f"tenant={tenant} users={count}")
    close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
