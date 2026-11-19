"""进程级配置：环境变量 + 可选的 .env。

只读配置，不在运行期改写。所有"新旧两套实现"的开关都必须在这里显式声明，
避免出现"以为在跑真的、其实跑的是假的"。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    """极简 .env 加载：只补缺失的键，不覆盖已有环境变量。"""
    path = PROJECT_ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("'\"")


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


@dataclass
class Settings:
    # --- 基础 ---
    env: str = "dev"
    db_url: str = "postgresql://ao:ao@127.0.0.1:55433/ao"
    secret: str = "ao-dev-secret"
    packages_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "packages")
    # --- 包存储（P5-T07）---
    # local：只有本地内容寻址目录；s3：发布时上传，运行时按 s3:// 引用下载到本地缓存
    package_backend: str = "local"
    package_s3_bucket: str = ""
    package_s3_prefix: str = ""
    # 兼容 MinIO/OSS 等 S3 网关；留空走 AWS 默认 endpoint
    package_s3_endpoint: str = ""
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / ".ao")
    default_tenant: str = "default"

    # --- 执行 ---
    mock: bool = True
    executor: str = "langgraph"
    exec_slots: int = 24
    coding_timeout_seconds: float = 1800.0
    cancel_poll_seconds: float = 1.0
    var_backoff_seconds: float = 2.0

    # --- 网关 / 计费 ---
    gateway_url: str = ""
    gateway_master_key: str = ""
    # 打开后模型调用真的走 LiteLLM：每次 turn 签虚拟 key、预算交给网关硬限、
    # 账单从 spend log 同步。关掉则是本地价目表估算（第一档行为）。
    gateway_enabled: bool = False
    # 网关不可用时是否直连上游。默认 false（fail closed）：宁可不跑，也不产生
    # 无归因的账单。开启后会在事件里留 degraded 标记。
    gateway_fail_open: bool = False
    # spend log 落库有 2-4s 延迟（实测），所以同步是周期任务而不是调用后立即结算
    gateway_sync_interval: float = 60.0

    # --- 事件触发（P2-T18）---
    # 空值表示只在 dev 环境放行事件入站；生产必须配，否则裸开等于给所有人一个触发器
    event_token: str = ""

    # --- 页面托管（P4-T04）---
    # 页面短期 token 的签名密钥；空值退回 AO_EVENT_TOKEN，再退回 dev 固定值
    page_secret: str = ""

    # --- 运维接口（P5-T01）---
    # 主体目录的读写只对运维开放；空值表示仅 dev 放行（与事件入站同构）
    ops_token: str = ""

    # --- 扇出（P5）---
    # 一批扇出的总预算兜底 = 展开主体数 × budget_per_run；0 表示不限。
    # 与计划自身的 fanout_limit 是两级闸门：limit 管"人数"，这里管"钱"。
    fanout_budget_cap: float = 0.0

    # --- 创作面（P2-T11）---
    uploads_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "uploads")
    upload_max_mb: float = 10.0
    # 未压缩消息累计到这么多条才压一次，每次压最早的 N 条；逐轮压缩成本高且摘要易漂移
    planner_summary_after: int = 20
    planner_summary_batch: int = 10
    # 生成失败（结构/静态检查/冒烟）时的回退轮数；1 表示不重试
    authoring_repair_rounds: int = 3

    # --- DSH（移植自 sortie-deck，字段名保持一致便于对照） ---
    dsh_profile: str = "sdk"
    dsh_llm_provider: str = "deepseek-official"
    dsh_model: str = ""
    dsh_max_tokens: int = 0
    dsh_reasoning_effort: str = ""
    dsh_permission_mode: str = "workspace-write"
    dsh_home_root: str = ""
    dsh_api_key: str | None = None
    dsh_base_url: str | None = None
    dsh_request_timeout_seconds: float = 0.0
    dsh_session_log_upload: bool = False
    dsh_isolate_skills: bool = True
    dsh_isolate_home: bool = False
    dsh_sandbox_auto_fallback: bool = False
    dsh_sandbox_probe_ttl_seconds: float = 600.0
    dsh_sandbox_probe_timeout_seconds: float = 30.0
    dsh_sandbox_probe_prompt: str = (
        "用 bash 执行 `echo __dsh_probe__`，然后只回答 ok。不要做其它事。"
    )
    dsh_sandbox_unavailable_hints: str = ""
    # 单个阶段内允许的 harness 轮数（含首轮）：产物不合格在同一会话里续轮修复
    stage_max_turns: int = 3
    # 阶段收尾是否用 LLM 提炼摘要写 memory.md（跨阶段记忆的唯一通道）
    stage_summary_enabled: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        _load_dotenv()
        return cls(
            env=os.environ.get("AO_ENV", "dev"),
            db_url=os.environ.get("AO_DB_URL", cls.db_url),
            secret=os.environ.get("AO_SECRET", "ao-dev-secret"),
            packages_dir=Path(os.environ.get("AO_PACKAGES_DIR", str(PROJECT_ROOT / "packages"))),
            package_backend=os.environ.get("AO_PACKAGE_BACKEND", "local"),
            package_s3_bucket=os.environ.get("AO_PACKAGE_S3_BUCKET", ""),
            package_s3_prefix=os.environ.get("AO_PACKAGE_S3_PREFIX", ""),
            package_s3_endpoint=os.environ.get("AO_PACKAGE_S3_ENDPOINT", ""),
            data_dir=Path(os.environ.get("AO_DATA_DIR", str(PROJECT_ROOT / ".ao"))),
            default_tenant=os.environ.get("AO_DEFAULT_TENANT", "default"),
            mock=_flag("AO_MOCK", True),
            executor=os.environ.get("AO_EXECUTOR", "langgraph"),
            exec_slots=_int("AO_EXEC_SLOTS", 24),
            coding_timeout_seconds=_float("AO_CODING_TIMEOUT", 1800.0),
            cancel_poll_seconds=_float("AO_CANCEL_POLL", 1.0),
            var_backoff_seconds=_float("AO_VAR_BACKOFF", 2.0),
            gateway_url=os.environ.get("AO_GATEWAY_URL", ""),
            gateway_master_key=os.environ.get("AO_GATEWAY_MASTER_KEY", ""),
            gateway_enabled=_flag("AO_GATEWAY_ENABLED", False),
            gateway_fail_open=_flag("AO_GATEWAY_FAIL_OPEN", False),
            gateway_sync_interval=_float("AO_GATEWAY_SYNC_INTERVAL", 60.0),
            # 注意用 or：.env 里写了空值时应回落到默认目录，而不是 Path(".")
            event_token=os.environ.get("AO_EVENT_TOKEN", ""),
            ops_token=os.environ.get("AO_OPS_TOKEN", ""),
            page_secret=os.environ.get("AO_PAGE_SECRET", ""),
            fanout_budget_cap=_float("AO_FANOUT_BUDGET_CAP", 0.0),
            uploads_dir=Path(os.environ.get("AO_UPLOADS_DIR") or str(PROJECT_ROOT / "uploads")),
            upload_max_mb=_float("AO_UPLOAD_MAX_MB", 10.0),
            planner_summary_after=_int("AO_PLANNER_SUMMARY_AFTER", 20),
            planner_summary_batch=_int("AO_PLANNER_SUMMARY_BATCH", 10),
            authoring_repair_rounds=_int("AO_AUTHORING_REPAIR_ROUNDS", 3),
            dsh_profile=os.environ.get("AO_DSH_PROFILE", "sdk"),
            dsh_llm_provider=os.environ.get("AO_DSH_LLM_PROVIDER", "deepseek-official"),
            dsh_model=os.environ.get("AO_DSH_MODEL", ""),
            dsh_max_tokens=_int("AO_DSH_MAX_TOKENS", 0),
            dsh_reasoning_effort=os.environ.get("AO_DSH_REASONING_EFFORT", ""),
            dsh_permission_mode=os.environ.get("AO_DSH_PERMISSION_MODE", "workspace-write"),
            dsh_home_root=os.environ.get("AO_DSH_HOME_ROOT", ""),
            dsh_api_key=(
                os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("AO_DSH_API_KEY") or None
            ),
            dsh_base_url=os.environ.get("AO_DSH_BASE_URL") or None,
            dsh_request_timeout_seconds=_float("AO_DSH_REQUEST_TIMEOUT", 0.0),
            dsh_session_log_upload=_flag("AO_DSH_SESSION_LOG_UPLOAD", False),
            dsh_isolate_skills=_flag("AO_DSH_ISOLATE_SKILLS", True),
            dsh_isolate_home=_flag("AO_DSH_ISOLATE_HOME", False),
            dsh_sandbox_auto_fallback=_flag("AO_DSH_SANDBOX_AUTO_FALLBACK", False),
            dsh_sandbox_probe_ttl_seconds=_float("AO_DSH_SANDBOX_PROBE_TTL", 600.0),
            dsh_sandbox_probe_timeout_seconds=_float("AO_DSH_SANDBOX_PROBE_TIMEOUT", 30.0),
            dsh_sandbox_probe_prompt=os.environ.get(
                "AO_DSH_SANDBOX_PROBE_PROMPT", cls.dsh_sandbox_probe_prompt
            ),
            dsh_sandbox_unavailable_hints=os.environ.get("AO_DSH_SANDBOX_UNAVAILABLE_HINTS", ""),
            stage_max_turns=_int("AO_STAGE_MAX_TURNS", 3),
            stage_summary_enabled=_flag("AO_STAGE_SUMMARY", True),
        )


settings = Settings.from_env()
