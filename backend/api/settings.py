from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.global_settings import GlobalSettings
from backend.schemas.global_settings import (
    GlobalSettingsUpdate,
    GlobalSettingsResponse,
    RuntimeSettingsUpdate,
    RuntimeSettingsResponse,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])


async def _get_or_create(db: AsyncSession) -> GlobalSettings:
    row = await db.get(GlobalSettings, 1)
    if not row:
        row = GlobalSettings(id=1)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


@router.get("/git", response_model=GlobalSettingsResponse)
async def get_git_settings(db: AsyncSession = Depends(get_db)):
    return await _get_or_create(db)


@router.put("/git", response_model=GlobalSettingsResponse)
async def update_git_settings(body: GlobalSettingsUpdate, db: AsyncSession = Depends(get_db)):
    row = await _get_or_create(db)
    for key, value in body.model_dump().items():
        setattr(row, key, value or None)
    await db.commit()
    await db.refresh(row)
    return row


def _pty_available() -> bool:
    try:
        import claude_pty.adapters.ccm  # noqa: F401
        return True
    except ImportError:
        return False


@router.get("/runtime", response_model=RuntimeSettingsResponse)
async def get_runtime_settings(db: AsyncSession = Depends(get_db)):
    from backend.main import instance_manager
    return RuntimeSettingsResponse(
        use_pty_mode=instance_manager.pty_mode_enabled,
        pty_available=_pty_available(),
    )


@router.put("/runtime", response_model=RuntimeSettingsResponse)
async def update_runtime_settings(
    body: RuntimeSettingsUpdate, db: AsyncSession = Depends(get_db)
):
    from backend.main import instance_manager

    effective = instance_manager.set_pty_mode(body.use_pty_mode)
    if not effective:
        # Reclaim idle PTY processes; mid-turn sessions finish first.
        drained = await instance_manager.drain_idle_pty_sessions()
        if drained:
            import logging
            logging.getLogger(__name__).info(
                "PTY mode off: drained %d idle session(s)", drained
            )
    row = await _get_or_create(db)
    row.use_pty_mode = effective
    await db.commit()
    return RuntimeSettingsResponse(
        use_pty_mode=effective,
        pty_available=_pty_available(),
    )
