"""页面生成器（P4）：Page Schema 契约 + 静态代码编译器。

- `schema`：受控 IR 与白名单校验；
- `compiler`：把 IR 编译成 `index.html` / `app.js` / `theme.css`，随包冻结。
"""

from ao.pages import compiler, schema

__all__ = ["compiler", "schema"]
