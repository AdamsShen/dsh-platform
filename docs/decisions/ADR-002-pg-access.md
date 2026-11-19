# ADR-002 数据访问：psycopg3 + 手写 SQL 迁移

- 状态：已实现
- 日期：2026-11-13（记录补写于 2026-11-19）
- 相关：P1-T06；`migrations/001_init.sql` ~ `011_gateway_keys.sql`

## 背景

MVP 用 SQLite 单文件，无法支撑 `FOR UPDATE SKIP LOCKED`、JSONB、声明式分区、物化视图——而这些恰好是租约队列、成本报表、事件归档的基础。

## 决策

1. 数据库只支持 PostgreSQL（本地用 docker compose 的 `postgres` 服务，生产为 PolarDB PG）。
2. 驱动用 **psycopg3**（`src/ao/db.py:15`）+ `psycopg_pool.ConnectionPool`（`:17`、`:24`），行工厂 `dict_row`。
3. 迁移用**手写 SQL 文件**，`src/ao/migrate.py` 按文件名顺序执行，幂等（`IF NOT EXISTS` / `ON CONFLICT`），无 ORM、无 Alembic。
4. 所有查询写显式 SQL，不引入查询构造器。

## 理由

- 队列语义（`SKIP LOCKED` + 租约 + fencing）与分区、物化视图都是 PG 原生能力，ORM 只会增加绕开成本。
- 手写 SQL 让"这一条语句锁了什么"可读，排队/接管/计费的竞态审查能直接看语句。
- 迁移文件编号即顺序，review 成本低；不引入 Alembic 的 autogenerate（会与手写分区、CHECK 冲突）。

## 替代方案

| 方案 | 否决原因 |
|---|---|
| SQLAlchemy + Alembic | 迁移 autogenerate 无法表达分区/物化视图；ORM 隐式 flush 让竞态难查 |
| 继续 SQLite + WAL | 无 `SKIP LOCKED`、无 JSONB 索引、无分区 |
| 引入 Redis 做队列 | 多一套有状态组件，且任务状态要双写 |

## 后果

- 正向：租约、幂等键、分区、报表都能直接在 SQL 里 review。
- 负向：跨库移植性为零（有意为之）；每次改表要同时改迁移、`manifest`、测试 fixtures。
- 约定：迁移**只前进不修改**；已发布迁移文件禁止编辑，修正用新文件（现有 011 个文件均遵循）。

## 验证

- `make migrate` 幂等（重复执行无副作用）。
- `uv run pytest -q` 中 `tests/integration/test_db.py` 覆盖建表与回滚。
