"""创作会话的文档上传：校验 → 落盘 → 抽文本。

文件名不可信：落盘名用内容 hash，原文件名只作为展示与槽位来源。
解析失败不静默返回空串——空文本会让后续生成"看起来成功、其实缺信息"。
"""

from __future__ import annotations

import os
from pathlib import Path

from ao.settings import settings
from ao.util import sha256_hex

# 只放行"能确定抽出文字"的类型；docx/图片/表格解析后续再加，避免假成功
ALLOWED_EXTS = {".md", ".markdown", ".txt", ".json", ".csv", ".yaml", ".yml", ".pdf"}
# 抽出的正文只保留头部，完整文件仍留在磁盘，需要时再读
MAX_TEXT_CHARS = 20000


class UploadError(Exception):
    """上传被拒：带稳定错误码，前端可直接展示原因。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _safe_name(filename: str) -> str:
    """拒绝带路径的文件名，防止 `../../etc/passwd` 这类穿越。"""
    raw = (filename or "").strip()
    if not raw:
        raise UploadError("empty_filename", "文件名不能为空")
    if "/" in raw or "\\" in raw or Path(raw).name != raw or raw.startswith("."):
        raise UploadError("bad_filename", f"文件名不合法: {raw!r}")
    return raw


def _session_dir(session_id: str) -> Path:
    # session_id 由平台生成（planner_ 前缀 + hex），这里再校验一次，防止拼路径
    if not session_id.replace("_", "").isalnum():
        raise UploadError("bad_session", f"session_id 不合法: {session_id!r}")
    path = settings.uploads_dir / session_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def extract_text(path: Path, ext: str) -> str:
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确指引
            raise UploadError("pdf_unsupported", "缺少 pypdf 依赖，无法解析 PDF") from exc
        try:
            reader = PdfReader(str(path))
            pages = [page.extract_text() or "" for page in reader.pages]
        except Exception as exc:  # noqa: BLE001 - 损坏/加密 PDF 一律判解析失败
            raise UploadError("pdf_parse_failed", f"PDF 解析失败: {exc}") from exc
        return "\n".join(pages).strip()
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        raise UploadError("read_failed", f"读取文件失败: {exc}") from exc


def save_upload(session_id: str, filename: str, data: bytes) -> dict:
    """落盘并返回附件元数据；同内容重复上传命同一个文件名（内容寻址）。"""
    name = _safe_name(filename)
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise UploadError("bad_type", f"不支持的文件类型 {ext or '(无扩展名)'}")
    limit = int(settings.upload_max_mb * 1024 * 1024)
    if len(data) > limit:
        raise UploadError("too_large", f"文件超过 {settings.upload_max_mb:g}MB 上限")
    if not data:
        raise UploadError("empty_file", "文件内容为空")

    digest = sha256_hex(data)
    directory = _session_dir(session_id)
    target = directory / f"{digest}{ext}"
    if not target.exists():
        tmp = directory / f".{digest}{ext}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, target)

    text = extract_text(target, ext)
    if not text:
        # 常见于扫描件 PDF：没有文本层，必须让人改传文字稿
        raise UploadError("no_text", "文件里没有可抽取的文字（扫描件请改传 md/txt）")

    return {
        "name": name,
        "sha256": digest,
        "bytes": len(data),
        "ext": ext,
        "path": f"{session_id}/{digest}{ext}",
        "chars": len(text),
        "excerpt": text[:4000],
        "text": text[:MAX_TEXT_CHARS] if ext != ".pdf" else text[:MAX_TEXT_CHARS],
    }
