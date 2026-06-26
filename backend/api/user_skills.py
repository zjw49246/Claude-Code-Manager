"""User Skills API — CRUD for user-created natural language skills."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.user_skill import UserSkill

router = APIRouter(prefix="/api/user-skills", tags=["user-skills"])


class SkillCreate(BaseModel):
    name: str
    description: str = ""
    content: str = ""
    sort_order: int = 0


class SkillUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    content: str | None = None
    sort_order: int | None = None


@router.get("")
async def list_skills(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(UserSkill).order_by(UserSkill.sort_order.desc(), UserSkill.id)
    )
    skills = result.scalars().all()
    return [
        {
            "id": s.id,
            "name": s.name,
            "description": s.description,
            "content": s.content,
            "sort_order": s.sort_order,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        }
        for s in skills
    ]


@router.get("/{skill_id}")
async def get_skill(skill_id: int, db: AsyncSession = Depends(get_db)):
    skill = await db.get(UserSkill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "content": skill.content,
        "sort_order": skill.sort_order,
        "created_at": skill.created_at.isoformat() if skill.created_at else None,
        "updated_at": skill.updated_at.isoformat() if skill.updated_at else None,
    }


@router.post("")
async def create_skill(body: SkillCreate, db: AsyncSession = Depends(get_db)):
    skill = UserSkill(**body.model_dump())
    db.add(skill)
    await db.commit()
    await db.refresh(skill)
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "content": skill.content,
        "sort_order": skill.sort_order,
        "created_at": skill.created_at.isoformat() if skill.created_at else None,
        "updated_at": skill.updated_at.isoformat() if skill.updated_at else None,
    }


@router.put("/{skill_id}")
async def update_skill(skill_id: int, body: SkillUpdate, db: AsyncSession = Depends(get_db)):
    skill = await db.get(UserSkill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    for key, val in body.model_dump(exclude_unset=True).items():
        setattr(skill, key, val)
    await db.commit()
    await db.refresh(skill)
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "content": skill.content,
        "sort_order": skill.sort_order,
        "created_at": skill.created_at.isoformat() if skill.created_at else None,
        "updated_at": skill.updated_at.isoformat() if skill.updated_at else None,
    }


@router.delete("/{skill_id}")
async def delete_skill(skill_id: int, db: AsyncSession = Depends(get_db)):
    skill = await db.get(UserSkill, skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found")
    await db.delete(skill)
    await db.commit()
    return {"ok": True}
