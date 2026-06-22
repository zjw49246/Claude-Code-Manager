from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.project import Project
from backend.models.project_todo import ProjectTodo
from backend.schemas.project_todo import ProjectTodoCreate, ProjectTodoResponse, ProjectTodoUpdate

router = APIRouter(prefix="/api/projects/{project_id}/todos", tags=["project-todos"])


async def _require_project(project_id: int, db: AsyncSession) -> Project:
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


async def _require_todo(project_id: int, todo_id: int, db: AsyncSession) -> ProjectTodo:
    result = await db.execute(
        select(ProjectTodo).where(ProjectTodo.id == todo_id, ProjectTodo.project_id == project_id)
    )
    todo = result.scalar_one_or_none()
    if not todo:
        raise HTTPException(404, "Todo not found")
    return todo


@router.get("", response_model=list[ProjectTodoResponse])
async def list_project_todos(
    project_id: int,
    include_archived: bool = False,
    db: AsyncSession = Depends(get_db),
):
    await _require_project(project_id, db)
    stmt = select(ProjectTodo).where(ProjectTodo.project_id == project_id)
    if not include_archived:
        stmt = stmt.where(ProjectTodo.status != "archived")
    result = await db.execute(
        stmt.order_by(desc(ProjectTodo.sort_order), desc(ProjectTodo.id))
    )
    return list(result.scalars().all())


@router.post("", response_model=ProjectTodoResponse, status_code=201)
async def create_project_todo(
    project_id: int,
    body: ProjectTodoCreate,
    db: AsyncSession = Depends(get_db),
):
    await _require_project(project_id, db)
    title = body.title.strip()
    prompt = body.prompt.strip()
    if not title or not prompt:
        raise HTTPException(400, "Title and prompt are required")

    # New todos go to the top: one step above the current max sort_order.
    max_sort_order = await db.scalar(
        select(func.coalesce(func.max(ProjectTodo.sort_order), 0)).where(ProjectTodo.project_id == project_id)
    )
    todo = ProjectTodo(
        project_id=project_id,
        title=title,
        prompt=prompt,
        status="open",
        sort_order=(max_sort_order or 0) + 100,
    )
    db.add(todo)
    await db.commit()
    await db.refresh(todo)
    return todo


@router.patch("/{todo_id}", response_model=ProjectTodoResponse)
async def update_project_todo(
    project_id: int,
    todo_id: int,
    body: ProjectTodoUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Partial update. Also the canonical way to archive (status='archived')
    or restore (status='open') — DELETE is reserved for permanent removal."""
    todo = await _require_todo(project_id, todo_id, db)
    updates = body.model_dump(exclude_unset=True)

    if "title" in updates and updates["title"] is not None:
        title = updates["title"].strip()
        if not title:
            raise HTTPException(400, "Title is required")
        todo.title = title
    if "prompt" in updates and updates["prompt"] is not None:
        prompt = updates["prompt"].strip()
        if not prompt:
            raise HTTPException(400, "Prompt is required")
        todo.prompt = prompt
    if "status" in updates and updates["status"] is not None:
        todo.status = updates["status"]
    if "sort_order" in updates and updates["sort_order"] is not None:
        todo.sort_order = updates["sort_order"]
    if "created_task_id" in updates:
        todo.created_task_id = updates["created_task_id"]
    # updated_at is bumped automatically by the model's onupdate=_utcnow on flush.

    await db.commit()
    await db.refresh(todo)
    return todo


@router.delete("/{todo_id}")
async def delete_project_todo(
    project_id: int,
    todo_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Permanently delete a todo. To hide without destroying, PATCH status='archived'."""
    todo = await _require_todo(project_id, todo_id, db)
    await db.delete(todo)
    await db.commit()
    return {"ok": True}
