"""真实 DSH 冒烟：默认跳过，`uv run pytest -m real` 才执行。

只验证"SDK 可用 + 鉴权通过 + 能拿到契约块"，不验证模型质量；
模型输出不稳定，断言只放在结构上。
"""

from __future__ import annotations

import re

import pytest

from ao.harness.dsh import DshProvider, sdk_available
from ao.settings import settings

pytestmark = pytest.mark.real

_RESULT_RE = re.compile(r"```result\s*(\{.*?\})\s*```", re.DOTALL)


@pytest.mark.skipif(not sdk_available(), reason="deepseek-harness-sdk 未安装")
@pytest.mark.skipif(not settings.dsh_api_key, reason="DEEPSEEK_API_KEY 未设置")
async def test_single_turn_returns_result_block(tmp_path) -> None:
    provider = DshProvider()
    reply = await provider.send(
        run_id="pytest_real",
        node_id="probe",
        attempt=0,
        system="你是一个只负责按要求输出 JSON 的执行节点。",
        prompt=(
            "请只输出一个 ```result 代码块，内容为 "
            '{"ok": true}，不要调用任何工具，不要输出其它文字。'
        ),
        contract=None,
        context={"cwd": str(tmp_path), "capability": "read_only", "timeout": 120},
    )

    assert reply.status == "ok", (reply.meta or {}).get("error")
    assert _RESULT_RE.search(reply.text or ""), f"未拿到 result 块: {reply.text[:200]!r}"
