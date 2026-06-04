from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.quick_phrase import QuickPhrase
from backend.schemas.quick_phrase import QuickPhraseCreate, QuickPhraseUpdate

router = APIRouter(prefix="/api/quick-phrases", tags=["quick-phrases"])


@router.get("")
async def list_quick_phrases(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(QuickPhrase).order_by(QuickPhrase.sort_order.asc(), QuickPhrase.id.asc())
    )
    return [
        {
            "id": p.id,
            "label": p.label,
            "content": p.content,
            "sort_order": p.sort_order,
        }
        for p in result.scalars().all()
    ]


@router.post("", status_code=201)
async def create_quick_phrase(
    data: QuickPhraseCreate, db: AsyncSession = Depends(get_db)
):
    phrase = QuickPhrase(
        label=data.label,
        content=data.content,
        sort_order=data.sort_order,
    )
    db.add(phrase)
    await db.commit()
    await db.refresh(phrase)
    return {
        "id": phrase.id,
        "label": phrase.label,
        "content": phrase.content,
        "sort_order": phrase.sort_order,
    }


@router.put("/{phrase_id}")
async def update_quick_phrase(
    phrase_id: int,
    data: QuickPhraseUpdate,
    db: AsyncSession = Depends(get_db),
):
    phrase = await db.get(QuickPhrase, phrase_id)
    if not phrase:
        raise HTTPException(status_code=404, detail="Quick phrase not found")
    if data.label is not None:
        phrase.label = data.label
    if data.content is not None:
        phrase.content = data.content
    if data.sort_order is not None:
        phrase.sort_order = data.sort_order
    await db.commit()
    return {
        "id": phrase.id,
        "label": phrase.label,
        "content": phrase.content,
        "sort_order": phrase.sort_order,
    }


@router.delete("/{phrase_id}")
async def delete_quick_phrase(
    phrase_id: int, db: AsyncSession = Depends(get_db)
):
    phrase = await db.get(QuickPhrase, phrase_id)
    if not phrase:
        raise HTTPException(status_code=404, detail="Quick phrase not found")
    await db.delete(phrase)
    await db.commit()
    return {"ok": True}
