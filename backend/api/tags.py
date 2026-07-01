from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, update, or_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.tag import Tag
from backend.models.project import Project
from backend.schemas.tag import TagCreate, TagUpdate, TagResponse
from backend.api.deps import get_current_user_id, get_current_user_role

router = APIRouter(prefix="/api/tags", tags=["tags"])


@router.get("", response_model=list[TagResponse])
async def list_tags(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = get_current_user_id(request)
    user_role = get_current_user_role(request)
    stmt = select(Tag).order_by(Tag.name.asc())
    if user_role not in ("admin", "super_admin"):
        stmt = stmt.where(Tag.created_by == user_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("", response_model=TagResponse, status_code=201)
async def create_tag(body: TagCreate, request: Request, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(Tag).where(Tag.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(400, f"Tag '{body.name}' already exists")
    tag = Tag(name=body.name, color=body.color, created_by=get_current_user_id(request))
    db.add(tag)
    await db.commit()
    await db.refresh(tag)
    return tag


@router.put("/{tag_id}", response_model=TagResponse)
async def update_tag(tag_id: int, body: TagUpdate, request: Request, db: AsyncSession = Depends(get_db)):
    tag = await db.get(Tag, tag_id)
    if not tag:
        raise HTTPException(404, "Tag not found")

    user_id = get_current_user_id(request)
    user_role = get_current_user_role(request)
    if user_role not in ("admin", "super_admin") and tag.created_by != user_id:
        raise HTTPException(403, "Can only edit your own tags")

    old_name = tag.name

    if body.name is not None and body.name != old_name:
        dup = await db.execute(select(Tag).where(Tag.name == body.name))
        if dup.scalar_one_or_none():
            raise HTTPException(400, f"Tag '{body.name}' already exists")

        result = await db.execute(select(Project))
        for project in result.scalars().all():
            if old_name in (project.tags or []):
                new_tags = [body.name if t == old_name else t for t in project.tags]
                project.tags = new_tags

        tag.name = body.name

    if body.color is not None:
        tag.color = body.color

    await db.commit()
    await db.refresh(tag)
    return tag


@router.delete("/{tag_id}")
async def delete_tag(tag_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    tag = await db.get(Tag, tag_id)
    if not tag:
        raise HTTPException(404, "Tag not found")

    user_id = get_current_user_id(request)
    user_role = get_current_user_role(request)
    if user_role not in ("admin", "super_admin") and tag.created_by != user_id:
        raise HTTPException(403, "Can only delete your own tags")

    result = await db.execute(select(Project))
    for project in result.scalars().all():
        if tag.name in (project.tags or []):
            project.tags = [t for t in project.tags if t != tag.name]

    await db.delete(tag)
    await db.commit()
    return {"ok": True}
