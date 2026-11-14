from __future__ import annotations

from ao.db import tx
from ao.package import freeze_package, load_package
from ao.runs import create_run, ensure_workflow, publish_version
from ao.settings import PROJECT_ROOT

EXAMPLE = PROJECT_ROOT / "examples" / "daily_schedule"


def publish_example() -> dict:
    manifest_hash, _ = freeze_package(EXAMPLE)
    package = load_package(EXAMPLE)
    manifest = package.manifest
    with tx() as conn:
        conn.execute(
            "INSERT INTO tenants (id, name) VALUES ('default','default') ON CONFLICT DO NOTHING"
        )
    ensure_workflow(
        manifest["id"], tenant_id="default", name=manifest.get("name") or manifest["id"]
    )
    publish_version(
        manifest["id"],
        version=int(manifest["version"]),
        manifest_hash=manifest_hash,
        package_ref=str(EXAMPLE),
    )
    return manifest


def make_run(subject: dict | None = None, window: str = "w") -> str:
    manifest = publish_example()
    run_id, _ = create_run(
        tenant_id="default",
        workflow_id=manifest["id"],
        version=int(manifest["version"]),
        package_ref=str(EXAMPLE),
        subject=subject or {"user_id": "u_001", "name": "小林"},
        window=window,
    )
    return run_id
