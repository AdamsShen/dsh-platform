from __future__ import annotations

from pathlib import Path

import pytest

from ao.db import fetch_one
from ao.planner import session as planner
from ao.planner.documents import UploadError
from ao.settings import settings


@pytest.fixture
def uploads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """上传目录指向临时目录，避免测试间串文件，也不污染仓库。"""
    target = tmp_path / "uploads"
    monkeypatch.setattr(settings, "uploads_dir", target)
    return target


def test_rolling_summary_keeps_first_ten_turns_slots(uploads: Path) -> None:
    session = planner.create_session(tenant_id="default", user_id="u_001")

    # 前 10 轮给出频率与产物，后面 10 轮只是追问，验证摘要保留的是"当时的槽位"
    for index in range(20):
        text = "每小时给小林生成日报" if index < 10 else f"第 {index} 轮补充说明，没有新信息"
        planner.append_message(session["id"], "user", text)
        planner.append_message(session["id"], "assistant", f"收到第 {index} 轮")

    stored = planner.get_session(session["id"])
    assert stored["summarized_upto"] >= 10
    assert "每小时" in stored["summary"]
    assert "日报" in stored["summary"]
    # 摘要里带槽位快照，而不只是原文截断
    assert "当时的槽位" in stored["summary"]

    slots = stored["slots"]
    assert [item["value"] for item in slots["frequency"]] == ["每小时"]
    assert slots["frequency"][0]["source"] == "user"

    context = planner.build_context(session["id"], recent=3)
    assert context["summary"]
    assert "frequency: 每小时" in context["slots_text"]
    assert len(context["messages"]) == 3


def test_upload_markdown_is_content_addressed_and_referenced(uploads: Path) -> None:
    session = planner.create_session(tenant_id="default", user_id="u_001")
    attachment = planner.upload_document(
        session["id"], "需求说明.md", "每天 9:00 生成周报，输出 markdown".encode()
    )

    # 落盘名是内容 hash，不是用户给的名字（文件名不可信）
    stored_name = Path(attachment["path"]).name
    assert stored_name == f"{attachment['sha256']}.md"
    assert (uploads / attachment["path"]).read_text(encoding="utf-8").startswith("每天")

    context = planner.build_context(session["id"])
    assert context["documents"] == ["需求说明.md"]
    assert "需求说明.md" in context["messages"][-1]["content"]
    assert [item["value"] for item in context["slots"]["outputs"]] == ["周报", "markdown"]

    # 同内容重复上传不产生第二个文件
    again = planner.upload_document(session["id"], "需求说明副本.md", b"\xe6\xaf\x8f")
    assert again["sha256"] != attachment["sha256"] or again["path"] == attachment["path"]


def _pdf_bytes(text: str) -> bytes:
    """手搓一个最小可解析 PDF，避免为测试引入 reportlab。"""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
        b" /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


def test_upload_pdf_extracts_text(uploads: Path) -> None:
    session = planner.create_session(tenant_id="default", user_id="u_001")
    attachment = planner.upload_document(
        session["id"], "weekly-report.pdf", _pdf_bytes("weekly report for Xiaolin")
    )
    assert attachment["ext"] == ".pdf"
    assert "weekly report" in attachment["excerpt"]


@pytest.mark.parametrize(
    ("filename", "payload", "code"),
    [
        ("../evil.md", b"x", "bad_filename"),
        ("a.exe", b"x", "bad_type"),
        ("empty.md", b"", "empty_file"),
    ],
)
def test_upload_rejects_bad_input(
    uploads: Path, filename: str, payload: bytes, code: str
) -> None:
    session = planner.create_session(tenant_id="default", user_id="u_001")
    with pytest.raises(UploadError) as excinfo:
        planner.upload_document(session["id"], filename, payload)
    assert excinfo.value.code == code


def test_upload_rejects_oversize(uploads: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "upload_max_mb", 0.0001)
    session = planner.create_session(tenant_id="default", user_id="u_001")
    with pytest.raises(UploadError) as excinfo:
        planner.upload_document(session["id"], "big.md", b"x" * 1024)
    assert excinfo.value.code == "too_large"


def test_authoring_usage_is_separate_from_runs(uploads: Path) -> None:
    session = planner.create_session(tenant_id="default", user_id="u_001")
    planner.record_authoring_usage(
        session["id"], {"input_tokens": 100, "output_tokens": 20, "model": "mock-1"}
    )
    row = fetch_one(
        "SELECT purpose, run_id, node_id FROM llm_usage WHERE node_id = %s", (session["id"],)
    )
    assert row is not None
    assert row["purpose"] == "authoring"
    assert row["run_id"] is None
