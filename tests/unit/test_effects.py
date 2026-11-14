from __future__ import annotations

from ao.effects import params_hash


def test_params_hash_is_order_insensitive() -> None:
    assert params_hash({"a": 1, "b": 2}) == params_hash({"b": 2, "a": 1})


def test_params_hash_distinguishes_values() -> None:
    assert params_hash({"n": 1}) != params_hash({"n": 2})
    assert params_hash({"n": 1}) != params_hash({"n": "1"})


def test_params_hash_handles_non_json_values() -> None:
    # 路径之类不可 JSON 序列化的参数按 str 降级，不能因为类型不同就算两个键
    from pathlib import Path

    assert params_hash({"p": Path("/tmp/x")}) == params_hash({"p": "/tmp/x"})
