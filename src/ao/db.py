"""PG 访问层：连接池 + 两个上下文管理器。

- `read()`：只读、自动提交，用于列表/详情。
- `tx()`：显式事务，用于状态转移、领取任务、写事件。

不引 ORM：本项目的查询形状固定（队列、事件、账目），手写 SQL 更容易看清锁与隔离级别。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ao.settings import settings

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=settings.db_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


def close_pool() -> None:
    """测试与进程退出用；不关会让 pytest 挂住。"""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def read() -> Iterator[psycopg.Connection]:
    with get_pool().connection() as conn:
        conn.autocommit = True
        yield conn


@contextmanager
def tx() -> Iterator[psycopg.Connection]:
    """一个显式事务；异常回滚，正常提交。"""
    with get_pool().connection() as conn:
        conn.autocommit = False
        with conn.transaction():
            yield conn


def fetch_one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with read() as conn:
        return conn.execute(sql, params).fetchone()


def fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with read() as conn:
        return conn.execute(sql, params).fetchall()


def execute(sql: str, params: tuple[Any, ...] = ()) -> int:
    with tx() as conn:
        return conn.execute(sql, params).rowcount
