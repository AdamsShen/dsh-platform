"""节点产物契约：从回复里抽 ```result 块，按 outputs 声明做类型转换与校验。"""

from __future__ import annotations

import json
import re
from typing import Any

RESULT_RE = re.compile(r"```result\s*\n(.*?)\n```", re.S)


def extract(text: str) -> dict[str, Any] | None:
    """取最后一个 result 块；容忍块内前后空白。"""
    matches = RESULT_RE.findall(text or "")
    if not matches:
        return None
    raw = matches[-1].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n", ""}:
        return False
    raise ValueError(f"无法转成 bool: {value!r}")


def coerce(data: dict[str, Any], outputs: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    """按声明类型转换；返回 (结果, 错误列表)。未声明的字段原样保留。"""
    result: dict[str, Any] = dict(data)
    errors: list[str] = []
    for name, kind in outputs.items():
        if name not in data:
            errors.append(f"缺少字段 {name}")
            continue
        raw = data[name]
        try:
            if kind == "str":
                result[name] = str(raw)
            elif kind == "int":
                result[name] = int(raw)
            elif kind == "float":
                result[name] = float(raw)
            elif kind == "bool":
                result[name] = _as_bool(raw)
        except (TypeError, ValueError) as exc:
            errors.append(f"字段 {name} 类型应为 {kind}: {exc}")
    return result, errors


def validate(text: str, outputs: dict[str, str]) -> tuple[dict[str, Any] | None, list[str]]:
    data = extract(text)
    if data is None:
        return None, ["没有找到合法的 ```result 块"]
    result, errors = coerce(data, outputs)
    return result, errors


def repair_prompt(errors: list[str], outputs: dict[str, str]) -> str:
    wanted = ", ".join(f"{k}: {v}" for k, v in outputs.items()) or "（无声明字段）"
    lines = "\n".join(f"- {e}" for e in errors)
    return (
        "上一条回复的产物不合格，请只重新输出一个 ```result JSON 块，不要解释。\n"
        f"需要字段：{wanted}\n"
        f"问题：\n{lines}\n"
    )


def contract_of(outputs: dict[str, str]) -> dict[str, Any]:
    return {"required": dict(outputs)}
