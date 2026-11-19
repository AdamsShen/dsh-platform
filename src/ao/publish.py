"""发布流水线：结构 → manifest → 提示词文件 → AST → 冒烟 → 冻结 → 入库目录。

任一阶段失败都返回结构化错误，**不产生 published 版本**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ao import resources, static_check, store
from ao.manifest import parse
from ao.package import PackageError, freeze_package, load_package
from ao.pages.build import compile_all_pages
from ao.smoke import run_smoke


@dataclass
class PublishResult:
    ok: bool
    stage: str = ""
    issues: list[dict[str, Any]] = field(default_factory=list)
    manifest_hash: str | None = None
    package_ref: str | None = None
    manifest: dict[str, Any] | None = None
    smoke: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "stage": self.stage,
            "issues": self.issues,
            "manifest_hash": self.manifest_hash,
            "package_ref": self.package_ref,
        }


async def publish(
    root: Path, *, smoke: bool = True, user_id: str | None = None
) -> PublishResult:
    # 1. 包结构
    try:
        package = load_package(root, verify=False)
    except PackageError as exc:
        return PublishResult(
            ok=False, stage="package", issues=[{"code": "E_PACKAGE", "message": str(exc)}]
        )

    # 2. manifest
    try:
        manifest = parse(package.manifest)
    except ValueError as exc:
        return PublishResult(
            ok=False, stage="manifest", issues=[{"code": "E_MANIFEST", "message": str(exc)}]
        )

    # 2.5 工作流级绑定：引用必须存在且有权使用。
    # 只有知道"谁在发布"时才校验；包级测试不传 user_id，避免把 DB 依赖带进来。
    if user_id is not None:
        binding_issues = resources.check_bindings(manifest, user_id=user_id)
        if binding_issues:
            return PublishResult(ok=False, stage="bindings", issues=binding_issues)

    # 3. 提示词 / 契约文件必须在包内且不能越界
    missing: list[dict[str, Any]] = []
    for node in manifest.nodes:
        paths = (
            ("prompt", node.prompt),
            ("system", node.system),
            ("contract", node.contract),
        )
        for label, relative in paths:
            if not relative:
                continue
            try:
                target = package.path(relative)
            except PackageError as exc:
                missing.append({"code": "E_PATH", "message": str(exc), "node": node.id})
                continue
            if not target.exists():
                missing.append(
                    {"code": "E_FILE", "message": f"节点 {node.id} 的 {label} 不存在: {relative}"}
                )
    if missing:
        return PublishResult(ok=False, stage="files", issues=missing)

    # 4. 声明自洽性（graph.py 已不参与执行，检查对象换成 manifest + prompts 模板）
    issues = static_check.check_manifest(manifest, package.root)
    if issues:
        return PublishResult(
            ok=False, stage="static", issues=[issue.as_dict() for issue in issues]
        )

    # 5. 冒烟
    smoke_result = None
    if smoke:
        smoke_result = await run_smoke(package.root)
        if not smoke_result["ok"]:
            return PublishResult(
                ok=False,
                stage="smoke",
                issues=[
                    {
                        "code": "E_SMOKE",
                        "message": smoke_result.get("error") or "冒烟未通过",
                        "nodes": [
                            n for n in smoke_result["nodes"] if n["status"] != "succeeded"
                        ],
                    }
                ],
                smoke=smoke_result,
            )

    # 5.5 页面编译（P4-T09）：Schema → 静态代码，必须在 freeze 之前，产物才进 hash
    pages = compile_all_pages(package.root)
    if pages["issues"]:
        return PublishResult(
            ok=False,
            stage="pages",
            issues=pages["issues"],
            smoke=smoke_result,
        )

    # 6. 冻结 + 7. 落仓库
    manifest_hash, _ = freeze_package(package.root)
    package = load_package(package.root)
    ref = store.publish(package.root, package.manifest, manifest_hash)

    return PublishResult(
        ok=True,
        stage="published",
        manifest_hash=manifest_hash,
        package_ref=ref,
        manifest=package.manifest,
        smoke=smoke_result,
    )
