"""Page Schema v1：受控 IR 与校验（P4-T02 / M4a-W41）。

为什么手写校验而不用 pydantic 聚合：发布期需要「一次报全、每个问题带 E_* 错误码与
节点路径」，pydantic 的 ValidationError 面向开发者调试，错误码与 path 语义对不上
`E_COMPONENT / E_DATASOURCE / E_ACTION / E_EXPR / E_VERSION / E_REF` 这套契约。

三条硬约束（对应 P4 退出条件 3）：
1. 组件、数据源、动作都必须在白名单里，未知一律拒绝；
2. 表达式只允许 `$workflow/$selection/$filters/$user` 开头的路径，不落任意 JS；
3. 任何 `on*` 事件属性、`eval(`、`javascript:`、`<script` 直接拒绝——这是编译前
   的第一道闸，编译器里还有第二道（产物断言）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 1

#: 组件白名单（与 P4-T02 一致，改这里等于改契约，必须同步 STATUS）
COMPONENTS = frozenset(
    {
        "page",
        "tabs",
        "filter",
        "table",
        "list",
        "card",
        "detail",
        "form",
        "calendar",
        "timeline",
        "chart",
        "text",
        "image",
        "modal",
        "button",
    }
)

#: 数据源白名单：全部只读，写操作只能走动作
DATASOURCES = frozenset({"run.list", "run.get", "run.events", "artifact.read", "mcp.read"})

#: 动作白名单：写回只允许 node.invoke，其余是 run 级操作
ACTIONS = frozenset({"run.trigger", "run.cancel", "run.retry", "node.invoke"})

EXPR_ROOTS = ("workflow", "selection", "filters", "user")
MAX_PAGE_LIMIT = 200
FIELD_KINDS = frozenset({"text", "number", "date", "select", "boolean"})
COLUMN_FORMATS = frozenset({"text", "date", "status", "number", "link"})

_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_DATA_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_EXPR_RE = re.compile(r"^\$(workflow|selection|filters|user)(\.[A-Za-z_][A-Za-z0-9_]*)+$")
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_UNSAFE_VALUE_RE = re.compile(
    r"(eval\s*\(|new\s+Function|javascript:|<script|\son[a-z]+\s*=)", re.IGNORECASE
)
#: HTML 事件属性一律小写（`onclick`）；不能用 IGNORECASE，否则 `onRowClick` 会被误伤
_ON_KEY_RE = re.compile(r"^on[a-z]+$")

#: 每类组件允许出现的键；不在表里的键一律拒绝（防止 `onclick` 之类混进来）
BLOCK_KEYS: dict[str, frozenset[str]] = {
    "text": frozenset({"type", "id", "title", "visibleIf", "text"}),
    "table": frozenset(
        {"type", "id", "title", "visibleIf", "source", "columns", "pageSize", "onRowClick"}
    ),
    "filter": frozenset({"type", "id", "title", "visibleIf", "fields"}),
    "list": frozenset(
        {"type", "id", "title", "visibleIf", "source", "itemTitle", "itemSubtitle", "itemFields"}
    ),
    "card": frozenset({"type", "id", "title", "visibleIf", "source", "titleField", "fields"}),
    "detail": frozenset({"type", "id", "title", "visibleIf", "source", "fields"}),
    "calendar": frozenset(
        {"type", "id", "title", "visibleIf", "source", "titleField", "startField", "endField"}
    ),
    "timeline": frozenset(
        {"type", "id", "title", "visibleIf", "source", "titleField", "timeField", "detailField"}
    ),
    "form": frozenset({"type", "id", "title", "visibleIf", "fields", "submit"}),
    "button": frozenset({"type", "id", "title", "visibleIf", "label", "action"}),
    "image": frozenset({"type", "id", "title", "visibleIf", "src", "alt"}),
    "tabs": frozenset({"type", "id", "title", "visibleIf", "tabs"}),
    "modal": frozenset({"type", "id", "title", "visibleIf", "blocks"}),
    "chart": frozenset({"type", "id", "title", "visibleIf", "source", "kind", "xField", "yField"}),
}

#: 需要 `source` 的组件
_SOURCED = frozenset({"table", "list", "card", "detail", "calendar", "timeline", "chart"})
#: 需要 `fields` 的组件
_FIELDED = frozenset({"filter", "form", "card", "detail"})
_CARD_KINDS = frozenset({"bar", "line", "pie"})
_IMAGE_PREFIXES = ("assets/", "/assets/", "data:image/")


@dataclass(frozen=True)
class Issue:
    """一条校验问题：code 是契约里的错误码，path 定位到节点（如 blocks[1].columns[0]）。"""

    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


class PageSchemaError(ValueError):
    """一次携带全部问题的异常；`issues` 顺序稳定，便于生成快照。"""

    def __init__(self, issues: list[Issue]) -> None:
        self.issues = list(issues)
        head = self.issues[0] if self.issues else Issue("E_VERSION", "$", "页面 Schema 非法")
        super().__init__(f"{head.code}@{head.path}: {head.message}（共 {len(self.issues)} 个问题）")


def validate(raw: Any) -> list[Issue]:
    """校验一份 Page Schema，返回全部问题（空列表表示通过）。

    不抛异常：发布流水线要把问题一次性回给作者，而不是"改一个报下一个"。
    """
    issues: list[Issue] = []
    if not isinstance(raw, dict):
        return [Issue("E_VERSION", "$", "页面 Schema 必须是对象")]

    _scan_unsafe(raw, "$", issues)

    version = raw.get("page_schema_version")
    if version != SCHEMA_VERSION:
        issues.append(
            Issue("E_VERSION", "page_schema_version", f"只支持 {SCHEMA_VERSION}，收到 {version!r}")
        )

    page_id = raw.get("id")
    if not isinstance(page_id, str) or not _ID_RE.match(page_id):
        issues.append(Issue("E_REF", "id", f"页面 id 不合法: {page_id!r}"))

    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        issues.append(Issue("E_REF", "title", "title 必须是非空字符串"))

    layout = raw.get("layout", "stack")
    if layout not in {"stack", "sidebar"}:
        issues.append(Issue("E_COMPONENT", "layout", f"layout 只支持 stack/sidebar: {layout!r}"))

    _check_theme(raw.get("theme"), "theme", issues)
    data_names = _check_data(raw.get("data", {}), issues)
    blocks = raw.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        issues.append(Issue("E_REF", "blocks", "blocks 必须是非空数组"))
        return issues
    for index, block in enumerate(blocks):
        _check_block(block, f"blocks[{index}]", data_names, issues)
    return issues


def load(raw: Any) -> dict[str, Any]:
    """校验并返回原 Schema（不复制），有问题直接抛 `PageSchemaError`。"""
    issues = validate(raw)
    if issues:
        raise PageSchemaError(issues)
    return raw


def _check_theme(theme: Any, path: str, issues: list[Issue]) -> None:
    if theme is None:
        return
    if not isinstance(theme, dict):
        issues.append(Issue("E_COMPONENT", path, "theme 必须是对象"))
        return
    allowed = {"primary", "background", "text", "border"}
    for key in theme:
        if key not in allowed:
            issues.append(Issue("E_COMPONENT", f"{path}.{key}", f"theme 不支持键 {key!r}"))
    for key, value in theme.items():
        if key in allowed and (not isinstance(value, str) or not _HEX_RE.match(value)):
            issues.append(
                Issue("E_REF", f"{path}.{key}", f"主题色必须是 #RRGGBB: {value!r}")
            )


def _check_data(raw: Any, issues: list[Issue]) -> set[str]:
    """校验 data 段，返回已声明的数据源名集合（后续 block 只能引用这些名字）。"""
    if raw is None:
        return set()
    if not isinstance(raw, dict):
        issues.append(Issue("E_DATASOURCE", "data", "data 必须是对象"))
        return set()
    names: set[str] = set()
    for name, spec in raw.items():
        path = f"data.{name}"
        if not isinstance(name, str) or not _DATA_NAME_RE.match(name):
            issues.append(Issue("E_REF", path, f"数据源名不合法: {name!r}"))
            continue
        names.add(name)
        if not isinstance(spec, dict):
            issues.append(Issue("E_DATASOURCE", path, "数据源声明必须是对象"))
            continue
        unknown = set(spec) - {"source", "params", "limit"}
        for key in sorted(unknown):
            issues.append(Issue("E_DATASOURCE", f"{path}.{key}", f"不支持的键 {key!r}"))
        source = spec.get("source")
        if source not in DATASOURCES:
            issues.append(
                Issue("E_DATASOURCE", f"{path}.source", f"数据源不在白名单: {source!r}")
            )
        limit = spec.get("limit", MAX_PAGE_LIMIT)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_PAGE_LIMIT
        ):
            issues.append(
                Issue("E_DATASOURCE", f"{path}.limit", f"limit 必须是 1~{MAX_PAGE_LIMIT} 的整数")
            )
        params = spec.get("params")
        if params is not None:
            if not isinstance(params, dict):
                issues.append(Issue("E_DATASOURCE", f"{path}.params", "params 必须是对象"))
            else:
                for key, value in params.items():
                    _check_param_value(value, f"{path}.params.{key}", issues)
    return names


def _check_param_value(value: Any, path: str, issues: list[Issue]) -> None:
    """参数值只允许字面量或受控表达式；对象/数组一律拒绝（不给注入留缝）。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str):
            _check_expr(value, path, issues)
        return
    issues.append(Issue("E_EXPR", path, "参数值只允许字面量或 $ 表达式"))


def _check_expr(value: str, path: str, issues: list[Issue]) -> None:
    """以 `$` 开头的字符串必须是受控表达式；其余按普通字面量放过。"""
    if not value.startswith("$"):
        return
    if not _EXPR_RE.match(value):
        issues.append(
            Issue(
                "E_EXPR",
                path,
                f"只允许 ${'/'.join(EXPR_ROOTS)} 开头的路径表达式，收到 {value!r}",
            )
        )


def _check_block(block: Any, path: str, data_names: set[str], issues: list[Issue]) -> None:
    if not isinstance(block, dict):
        issues.append(Issue("E_COMPONENT", path, "block 必须是对象"))
        return
    kind = block.get("type")
    if kind not in COMPONENTS or kind == "page":
        issues.append(Issue("E_COMPONENT", f"{path}.type", f"组件不在白名单: {kind!r}"))
        return
    allowed = BLOCK_KEYS[kind]
    for key in sorted(set(block) - allowed):
        issues.append(Issue("E_COMPONENT", f"{path}.{key}", f"{kind} 不支持键 {key!r}"))

    if kind in _SOURCED:
        source = block.get("source")
        if not isinstance(source, str) or source not in data_names:
            issues.append(
                Issue("E_REF", f"{path}.source", f"source 未在 data 中声明: {source!r}")
            )

    if kind in _FIELDED:
        _check_fields(block.get("fields"), f"{path}.fields", issues)
        if kind in {"card", "detail"} and not isinstance(block.get("titleField"), str):
            # card/detail 允许用 fields 表达，不强求 titleField
            pass
    if kind == "table":
        _check_columns(block.get("columns"), f"{path}.columns", issues)
        page_size = block.get("pageSize", 50)
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= MAX_PAGE_LIMIT
        ):
            issues.append(
                Issue("E_DATASOURCE", f"{path}.pageSize", f"pageSize 必须是 1~{MAX_PAGE_LIMIT}")
            )
        click = block.get("onRowClick")
        if click is not None:
            if not isinstance(click, dict) or set(click) - {"source", "param"}:
                issues.append(
                    Issue("E_REF", f"{path}.onRowClick", "onRowClick 只支持 source/param")
                )
            else:
                target = click.get("source")
                if not isinstance(target, str) or target not in data_names:
                    issues.append(
                        Issue("E_REF", f"{path}.onRowClick.source", f"未声明的数据源: {target!r}")
                    )
                param = click.get("param", "id")
                if not isinstance(param, str) or not param:
                    issues.append(Issue("E_REF", f"{path}.onRowClick.param", "param 不能为空"))
    if kind == "text":
        if not isinstance(block.get("text"), str):
            issues.append(Issue("E_COMPONENT", f"{path}.text", "text 必须是字符串"))
    if kind == "image":
        src = block.get("src")
        if not isinstance(src, str) or not src.startswith(_IMAGE_PREFIXES):
            issues.append(
                Issue("E_REF", f"{path}.src", "图片只允许 assets/ 或 data:image/ 前缀")
            )
    if kind == "button":
        if not isinstance(block.get("label"), str) or not block["label"].strip():
            issues.append(Issue("E_COMPONENT", f"{path}.label", "按钮必须有 label"))
        _check_action(block.get("action"), f"{path}.action", issues)
    if kind == "form":
        _check_action(block.get("submit"), f"{path}.submit", issues)
    if kind == "tabs":
        tabs = block.get("tabs")
        if not isinstance(tabs, list) or not tabs:
            issues.append(Issue("E_COMPONENT", f"{path}.tabs", "tabs 必须是非空数组"))
        else:
            for index, tab in enumerate(tabs):
                tab_path = f"{path}.tabs[{index}]"
                if not isinstance(tab, dict) or set(tab) - {"label", "blocks"}:
                    issues.append(Issue("E_COMPONENT", tab_path, "tab 只支持 label/blocks"))
                    continue
                if not isinstance(tab.get("label"), str):
                    issues.append(Issue("E_COMPONENT", f"{tab_path}.label", "label 必须是字符串"))
                inner = tab.get("blocks")
                if not isinstance(inner, list):
                    issues.append(Issue("E_COMPONENT", f"{tab_path}.blocks", "blocks 必须是数组"))
                else:
                    for inner_index, inner_block in enumerate(inner):
                        _check_block(
                            inner_block, f"{tab_path}.blocks[{inner_index}]", data_names, issues
                        )
    if kind == "modal":
        inner = block.get("blocks")
        if not isinstance(inner, list) or not inner:
            issues.append(Issue("E_COMPONENT", f"{path}.blocks", "modal 必须有 blocks"))
        else:
            for inner_index, inner_block in enumerate(inner):
                _check_block(inner_block, f"{path}.blocks[{inner_index}]", data_names, issues)
    if kind == "chart":
        if block.get("kind") not in _CARD_KINDS:
            issues.append(Issue("E_COMPONENT", f"{path}.kind", "chart.kind 只支持 bar/line/pie"))
        for key in ("xField", "yField"):
            if not isinstance(block.get(key), str):
                issues.append(Issue("E_COMPONENT", f"{path}.{key}", f"{key} 必须是字符串"))
    if kind == "calendar":
        for key in ("titleField", "startField", "endField"):
            if not isinstance(block.get(key), str):
                issues.append(Issue("E_COMPONENT", f"{path}.{key}", f"{key} 必须是字符串"))
    if kind == "timeline":
        for key in ("titleField", "timeField"):
            if not isinstance(block.get(key), str):
                issues.append(Issue("E_COMPONENT", f"{path}.{key}", f"{key} 必须是字符串"))

    visible_if = block.get("visibleIf")
    if visible_if is not None:
        if not isinstance(visible_if, str):
            issues.append(Issue("E_EXPR", f"{path}.visibleIf", "visibleIf 必须是表达式字符串"))
        else:
            _check_expr(visible_if, f"{path}.visibleIf", issues)


def _check_fields(fields: Any, path: str, issues: list[Issue]) -> None:
    if fields is None:
        return
    if not isinstance(fields, list) or not fields:
        issues.append(Issue("E_COMPONENT", path, "fields 必须是非空数组"))
        return
    seen: set[str] = set()
    for index, field in enumerate(fields):
        item = f"{path}[{index}]"
        if not isinstance(field, dict):
            issues.append(Issue("E_COMPONENT", item, "字段必须是对象"))
            continue
        unknown = set(field) - {"name", "label", "kind", "required", "options", "format"}
        for key in sorted(unknown):
            issues.append(Issue("E_COMPONENT", f"{item}.{key}", f"字段不支持键 {key!r}"))
        name = field.get("name")
        if not isinstance(name, str) or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            issues.append(Issue("E_REF", f"{item}.name", f"字段名不合法: {name!r}"))
        elif name in seen:
            issues.append(Issue("E_REF", f"{item}.name", f"字段名重复: {name}"))
        else:
            seen.add(name)
        if not isinstance(field.get("label"), str) or not field["label"].strip():
            issues.append(Issue("E_COMPONENT", f"{item}.label", "字段必须有 label"))
        kind = field.get("kind", "text")
        if kind not in FIELD_KINDS:
            issues.append(Issue("E_COMPONENT", f"{item}.kind", f"字段类型不支持: {kind!r}"))
        if kind == "select":
            options = field.get("options")
            if not isinstance(options, list) or not options:
                issues.append(Issue("E_COMPONENT", f"{item}.options", "select 必须有 options"))
            else:
                for opt_index, option in enumerate(options):
                    opt_path = f"{item}.options[{opt_index}]"
                    if not isinstance(option, dict) or set(option) - {"label", "value"}:
                        issues.append(Issue("E_COMPONENT", opt_path, "option 只支持 label/value"))
        fmt = field.get("format")
        if fmt is not None and fmt not in COLUMN_FORMATS:
            issues.append(Issue("E_COMPONENT", f"{item}.format", f"format 不支持: {fmt!r}"))


def _check_columns(columns: Any, path: str, issues: list[Issue]) -> None:
    if not isinstance(columns, list) or not columns:
        issues.append(Issue("E_COMPONENT", path, "columns 必须是非空数组"))
        return
    seen: set[str] = set()
    for index, column in enumerate(columns):
        item = f"{path}[{index}]"
        if not isinstance(column, dict):
            issues.append(Issue("E_COMPONENT", item, "列必须是对象"))
            continue
        unknown = set(column) - {"field", "label", "width", "format"}
        for key in sorted(unknown):
            issues.append(Issue("E_COMPONENT", f"{item}.{key}", f"列不支持键 {key!r}"))
        field_name = column.get("field")
        if not isinstance(field_name, str) or not field_name:
            issues.append(Issue("E_REF", f"{item}.field", "列必须有 field"))
        elif field_name in seen:
            issues.append(Issue("E_REF", f"{item}.field", f"列重复: {field_name}"))
        else:
            seen.add(field_name)
        if not isinstance(column.get("label"), str) or not column["label"].strip():
            issues.append(Issue("E_COMPONENT", f"{item}.label", "列必须有 label"))
        fmt = column.get("format", "text")
        if fmt not in COLUMN_FORMATS:
            issues.append(Issue("E_COMPONENT", f"{item}.format", f"format 不支持: {fmt!r}"))
        width = column.get("width")
        if width is not None and (
            isinstance(width, bool) or not isinstance(width, int) or width <= 0
        ):
            issues.append(Issue("E_COMPONENT", f"{item}.width", "width 必须是正整数"))


def _check_action(action: Any, path: str, issues: list[Issue]) -> None:
    if not isinstance(action, dict):
        issues.append(Issue("E_ACTION", path, "动作必须是对象"))
        return
    unknown = set(action) - {"action", "node", "run", "confirm", "payload"}
    for key in sorted(unknown):
        issues.append(Issue("E_ACTION", f"{path}.{key}", f"动作不支持键 {key!r}"))
    name = action.get("action")
    if name not in ACTIONS:
        issues.append(Issue("E_ACTION", f"{path}.action", f"动作不在白名单: {name!r}"))
        return
    if name == "node.invoke":
        node = action.get("node")
        if not isinstance(node, str) or not _ID_RE.match(node):
            issues.append(Issue("E_ACTION", f"{path}.node", "node.invoke 必须声明合法 node"))
    if "run" in action:
        run = action.get("run")
        if not isinstance(run, str):
            issues.append(Issue("E_EXPR", f"{path}.run", "run 必须是表达式字符串"))
        else:
            _check_expr(run, f"{path}.run", issues)
    confirm = action.get("confirm")
    if confirm is not None and not isinstance(confirm, bool):
        issues.append(Issue("E_ACTION", f"{path}.confirm", "confirm 必须是布尔值"))
    payload = action.get("payload")
    if payload is not None and not isinstance(payload, dict):
        issues.append(Issue("E_ACTION", f"{path}.payload", "payload 必须是对象"))


def _scan_unsafe(node: Any, path: str, issues: list[Issue]) -> None:
    """递归拒绝事件属性与可执行片段。放在最前面，避免被后续类型判断掩盖。"""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path != "$" else key
            if isinstance(key, str) and _ON_KEY_RE.match(key):
                issues.append(
                    Issue("E_EXPR", child, f"不接受事件属性 {key!r}（动作请走 action 白名单）")
                )
            _scan_unsafe(value, child, issues)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _scan_unsafe(value, f"{path}[{index}]", issues)
    elif isinstance(node, str):
        if _UNSAFE_VALUE_RE.search(node):
            issues.append(Issue("E_EXPR", path, f"字符串含可执行片段: {node[:40]!r}"))
