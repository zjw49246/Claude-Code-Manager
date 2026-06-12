"""Phase 1 真机冒烟：收养一台已有 EC2 跑完整 bootstrap pipeline。

用法（在仓库根目录）:
    WORKER_SSH_KEY_PATH=~/.ssh/xxx.pem \
    .venv/bin/python scripts/worker_phase1_smoke.py --adopt i-xxxx [--db /tmp/x.db]

不碰生产/开发 DB：默认用独立 sqlite。结束后打印 worker 状态 + bootstrap 日志，
并验证 Worker CCM 的 /api/system/health（含 commit 版本锁定校验）。
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--adopt", required=True, help="收养的 EC2 instance id")
    parser.add_argument("--db", default="/tmp/worker_smoke.db")
    args = parser.parse_args()

    os.environ.setdefault("WORKER_ENABLED", "true")
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{args.db}"

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

    from backend.database import Base
    import backend.models.worker  # noqa: F401  (注册到 Base.metadata)
    from backend.models.worker import Worker
    from backend.config import settings
    from backend.services.cloud_provider import get_cloud_provider
    from backend.services.worker_provisioner import WorkerProvisioner

    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as db:
        worker = Worker(
            name="smoke-worker",
            status="creating",
            ssh_user=settings.worker_ssh_user,
            ssh_key_path=settings.worker_ssh_key_path,
        )
        db.add(worker)
        await db.commit()
        await db.refresh(worker)
        wid = worker.id

    prov = WorkerProvisioner(
        db_factory=factory, cloud=get_cloud_provider("aws"), broadcaster=None
    )
    print(f"== adopting {args.adopt} as worker {wid} ==")
    await prov.create_worker(wid, accounts=[], adopt_instance_id=args.adopt)

    async with factory() as db:
        w = await db.get(Worker, wid)
    print("\n== bootstrap log ==")
    print(w.bootstrap_log)
    print(f"== status={w.status} step={w.bootstrap_step} error={w.bootstrap_error}")
    print(f"== private_ip={w.private_ip} commit={w.ccm_commit} token={w.auth_token[:8] if w.auth_token else None}...")

    if w.status == "ready":
        import httpx
        url = f"http://{w.private_ip}:{w.ccm_port}/api/system/health"
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url, headers={"Authorization": f"Bearer {w.auth_token}"})
        print(f"== health: {r.status_code} {r.json()}")
        ok = r.json().get("commit") == w.ccm_commit
        print(f"== commit 版本锁定校验: {'PASS' if ok else 'MISMATCH'}")

    await engine.dispose()
    return 0 if w.status == "ready" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
