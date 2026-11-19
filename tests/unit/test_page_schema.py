"""W41：Page Schema v1 校验——每个 E_* 错误码至少一例，合法看板必须通过。"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from ao.pages import schema as page_schema


def board() -> dict[str, Any]:
    """日程看板：筛选 + 表格 + 详情 + 时间线 + 写操作，覆盖常用组件。"""
    return {
        "page_schema_version": 1,
        "id": "daily_board",
        "title": "每日日程",
        "layout": "stack",
        "theme": {"primary": "#2f6fed", "background": "#f7f8fa"},
        "data": {
            "runs": {
                "source": "run.list",
                "params": {"status": "$filters.status"},
                "limit": 50,
            },
            "run": {"source": "run.get", "params": {"id": "$selection.id"}},
            "events": {"source": "run.events", "params": {"id": "$selection.id"}},
        },
        "blocks": [
            {
                "type": "filter",
                "fields": [
                    {
                        "name": "status",
                        "label": "状态",
                        "kind": "select",
                        "options": [
                            {"label": "全部", "value": ""},
                            {"label": "成功", "value": "succeeded"},
                        ],
                    },
                    {"name": "keyword", "label": "关键词", "kind": "text"},
                ],
            },
            {
                "type": "table",
                "title": "运行记录",
                "source": "runs",
                "pageSize": 20,
                "columns": [
                    {"field": "id", "label": "ID", "format": "text"},
                    {"field": "created_at", "label": "创建时间", "format": "date"},
                ],
                "onRowClick": {"source": "run", "param": "id"},
            },
            {
                "type": "detail",
                "title": "详情",
                "source": "run",
                "fields": [
                    {"name": "status", "label": "状态"},
                    {"name": "spent_usd", "label": "花费"},
                ],
            },
            {
                "type": "timeline",
                "title": "事件",
                "source": "events",
                "titleField": "type",
                "timeField": "created_at",
                "detailField": "node_id",
            },
            {
                "type": "form",
                "title": "补一条备注",
                "fields": [{"name": "note", "label": "备注", "required": True}],
                "submit": {"action": "node.invoke", "node": "save_note", "confirm": True},
            },
            {
                "type": "button",
                "label": "重跑",
                "action": {"action": "run.retry", "run": "$selection.id", "confirm": True},
            },
        ],
    }


def codes(raw: dict[str, Any]) -> set[str]:
    return {issue.code for issue in page_schema.validate(raw)}


def paths(raw: dict[str, Any], code: str) -> set[str]:
    return {issue.path for issue in page_schema.validate(raw) if issue.code == code}


def test_valid_board_passes() -> None:
    assert page_schema.validate(board()) == []
    assert page_schema.load(board())["id"] == "daily_board"


def test_valid_nested_tabs_and_card() -> None:
    raw = board()
    raw["blocks"] = [
        {
            "type": "tabs",
            "tabs": [
                {
                    "label": "卡片",
                    "blocks": [
                        {
                            "type": "card",
                            "source": "runs",
                            "titleField": "id",
                            "fields": [{"name": "status", "label": "状态"}],
                        }
                    ],
                }
            ],
        }
    ]
    assert page_schema.validate(raw) == []


def test_e_version() -> None:
    raw = board()
    raw["page_schema_version"] = 2
    assert codes(raw) == {"E_VERSION"}
    assert codes("not-a-dict") == {"E_VERSION"}  # type: ignore[arg-type]


def test_e_component_unknown_type_and_extra_key() -> None:
    raw = board()
    raw["blocks"][0] = {"type": "iframe", "src": "https://evil"}
    assert "E_COMPONENT" in codes(raw)

    raw = board()
    raw["blocks"][1]["onclick"] = "alert(1)"
    issues = page_schema.validate(raw)
    assert {issue.code for issue in issues} >= {"E_COMPONENT", "E_EXPR"}
    assert "blocks[1].onclick" in {issue.path for issue in issues}


def test_e_datasource_unknown_and_bad_limit() -> None:
    raw = board()
    raw["data"]["runs"]["source"] = "run.delete"
    assert paths(raw, "E_DATASOURCE") == {"data.runs.source"}

    raw = board()
    raw["data"]["runs"]["limit"] = 500
    assert paths(raw, "E_DATASOURCE") == {"data.runs.limit"}

    raw = board()
    raw["data"]["runs"]["sql"] = "select 1"
    assert "E_DATASOURCE" in codes(raw)


def test_e_action_whitelist_and_node_invoke() -> None:
    raw = board()
    raw["blocks"][5]["action"]["action"] = "run.delete"
    assert paths(raw, "E_ACTION") == {"blocks[5].action.action"}

    raw = board()
    raw["blocks"][4]["submit"] = {"action": "node.invoke"}
    assert paths(raw, "E_ACTION") == {"blocks[4].submit.node"}


def test_e_expr_only_whitelisted_roots() -> None:
    raw = board()
    raw["data"]["runs"]["params"]["status"] = "$filters['status']"
    assert paths(raw, "E_EXPR") == {"data.runs.params.status"}

    raw = board()
    raw["blocks"][1]["visibleIf"] = "$root.admin"
    assert paths(raw, "E_EXPR") == {"blocks[1].visibleIf"}


def test_e_ref_undeclared_source_and_id() -> None:
    raw = board()
    raw["blocks"][1]["source"] = "missing"
    assert paths(raw, "E_REF") == {"blocks[1].source"}

    raw = board()
    raw["id"] = "Daily Board!"
    assert paths(raw, "E_REF") == {"id"}

    raw = board()
    raw["blocks"][1]["columns"].append({"field": "id", "label": "重复"})
    assert "blocks[1].columns[2].field" in paths(raw, "E_REF")


def test_unsafe_payload_rejected() -> None:
    for payload in ("alert(1); eval('x')", "javascript:alert(1)", "<script>x</script>"):
        raw = board()
        raw["blocks"][0]["fields"][1]["label"] = payload
        assert "E_EXPR" in codes(raw), payload


def test_image_src_must_be_local_or_data_uri() -> None:
    raw = board()
    raw["blocks"].append({"type": "image", "src": "https://evil.example/x.png"})
    assert paths(raw, "E_REF") == {"blocks[6].src"}

    raw = board()
    raw["blocks"].append({"type": "image", "src": "assets/logo.png", "alt": "logo"})
    assert page_schema.validate(raw) == []


def test_load_raises_with_all_issues() -> None:
    raw = board()
    raw["page_schema_version"] = 9
    raw["id"] = "BAD ID"
    with pytest.raises(page_schema.PageSchemaError) as exc:
        page_schema.load(raw)
    assert {issue.code for issue in exc.value.issues} == {"E_VERSION", "E_REF"}


def test_validate_does_not_mutate_input() -> None:
    raw = board()
    snapshot = copy.deepcopy(raw)
    page_schema.validate(raw)
    assert raw == snapshot
