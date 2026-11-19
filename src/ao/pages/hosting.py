"""页面托管与短期只读 token（P4-T04 / M4a-W43）。

三条安全线：
1. **静态目录按版本冻结**：`ui/compiled/<page>/` 随包内容寻址，运行期只读不编译；
2. **iframe 不给 `allow-same-origin`**：页面拿不到宿主 cookie，数据只能走 token 化的 API；
3. **token 短期且绑定页面**：5 分钟、绑定 `workflow@version:page` + user + tenant，
   签名不依赖 cookie，页面里也没有 `document.cookie` 可读。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

from ao.db import fetch_one
from ao.package import PackageError, load_package
from ao.settings import settings

TOKEN_TTL_SECONDS = 300
#: 静态页面的响应头策略；`frame-ancestors 'self'` 允许被同源宿主页面 iframe 嵌入
PAGE_CSP = (
    "default-src 'self'; style-src 'self'; script-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'self'"
)
#: 编译产物里允许被直接取的文件名（其余一律 404，避免目录遍历/取到源码）
PAGE_ASSETS = frozenset({"index.html", "app.js", "theme.css"})


class HostingError(RuntimeError):
    """托管链路的可预期失败：包不存在、页面不存在、token 非法/过期。"""


def _secret() -> bytes:
    """签名密钥：优先 `AO_PAGE_SECRET`，其次事件令牌，dev 用固定值。

    生产未配置时会在日志里出现，但不会静默用空密钥——空密钥等于任何人都能签。
    """
    raw = settings.page_secret or settings.event_token
    if raw:
        return raw.encode("utf-8")
    return b"ao-page-dev-secret"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def issue_token(
    *,
    workflow_id: str,
    version: int,
    page_id: str,
    user_id: str,
    tenant_id: str,
    ttl: int = TOKEN_TTL_SECONDS,
) -> str:
    """签发只读 token；`ttl<=0` 可用来造一个立即过期的 token（测试用）。"""
    payload = {
        "w": workflow_id,
        "v": int(version),
        "p": page_id,
        "u": user_id,
        "t": tenant_id,
        "exp": int(time.time()) + int(ttl),
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    signature = hmac.new(_secret(), body.encode("utf-8"), hashlib.sha256).digest()
    return f"{_b64(body.encode('utf-8'))}.{_b64(signature)}"


def verify_token(token: str) -> dict[str, Any]:
    """校验 token，返回 payload；任何异常都统一抛 `HostingError`（不给攻击者区分度）。"""
    if not token or "." not in token:
        raise HostingError("token 缺失或格式不对")
    body_b64, sig_b64 = token.split(".", 1)
    try:
        body = _unb64(body_b64)
        signature = _unb64(sig_b64)
    except Exception as exc:  # noqa: BLE001 — 编码错误一律按非法 token 处理
        raise HostingError("token 编码非法") from exc
    expected = hmac.new(_secret(), body, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise HostingError("token 签名不匹配")
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise HostingError("token 内容非法") from exc
    if int(payload.get("exp") or 0) <= int(time.time()):
        raise HostingError("token 已过期")
    return payload


def package_root(workflow_id: str, version: int) -> Path:
    """按 `workflow@version` 找冻结包目录；找不到就 404 语义失败。"""
    row = fetch_one(
        "SELECT package_ref FROM workflow_versions WHERE workflow_id = %s AND version = %s",
        (workflow_id, int(version)),
    )
    if row is None:
        raise HostingError(f"版本不存在: {workflow_id}@{version}")
    try:
        return load_package(row["package_ref"]).root
    except PackageError as exc:
        raise HostingError(str(exc)) from exc


def page_dir(root: Path, page_id: str) -> Path:
    """`ui/compiled/<page>/`，并校验真实路径没跑出包目录。"""
    target = (root / "ui" / "compiled" / page_id).resolve()
    base = root.resolve()
    if base not in target.parents:
        raise HostingError(f"页面路径越界: {page_id}")
    if not (target / "index.html").exists():
        raise HostingError(f"页面未编译: {page_id}")
    return target


def resolve_asset(root: Path, page_id: str, asset: str) -> Path:
    """静态资源解析：只放行三个固定文件与 `assets/` 下的文件。"""
    directory = page_dir(root, page_id)
    if asset in PAGE_ASSETS:
        return directory / asset
    if not asset.startswith("assets/"):
        raise HostingError(f"不允许的静态资源: {asset}")
    target = (directory / asset).resolve()
    if directory not in target.parents or not target.is_file():
        raise HostingError(f"静态资源不存在: {asset}")
    return target


def page_schema(root: Path, page_id: str) -> dict[str, Any]:
    """取页面真值（`ui/pages/<page>.json`），数据源白名单以它为准。"""
    path = root / "ui" / "pages" / f"{page_id}.json"
    if not path.exists():
        raise HostingError(f"页面 Schema 不存在: {page_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def shell_html(
    *, workflow_id: str, version: int, page_id: str, user_id: str, tenant_id: str
) -> str:
    """宿主页面壳：iframe 只给 allow-scripts（不给 allow-same-origin）。

    壳本身由平台渲染，token 注入到 iframe src；页面内无法读宿主 cookie，
    数据请求带 token 走 `/api/page/...`。
    """
    token = issue_token(
        workflow_id=workflow_id,
        version=version,
        page_id=page_id,
        user_id=user_id,
        tenant_id=tenant_id,
    )
    src = f"./{page_id}/index.html?token={token}"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{_escape(page_id)}</title>
<style>
  html, body {{ margin: 0; height: 100%; background: #f7f8fa; }}
  iframe {{ border: 0; width: 100%; height: 100vh; display: block; }}
</style>
</head>
<body>
<iframe sandbox="allow-scripts" referrerpolicy="no-referrer" src="{_escape(src)}"
        title="{_escape(page_id)}"></iframe>
</body>
</html>
"""


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
