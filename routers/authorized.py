"""CRUD de Usuários Autorizados — aba administrativa."""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import AdminAction, AuthorizedUser, User
from routers.board import _resolve_church
from services.admin_commands import ALL_PERMISSIONS, PROFILE_DEFAULTS, get_effective_permissions

router = APIRouter(prefix="/api/authorized", tags=["authorized"])


# ── Schemas ──────────────────────────────────────────────────────────

class AuthUserCreate(BaseModel):
    name: str = Field(max_length=120)
    phone: str = Field(max_length=20)
    profile: str = Field(default="operador")
    allowed_departments: str = ""
    allowed_groups: str = ""
    permissions: str = ""
    notes: str = ""


class AuthUserUpdate(BaseModel):
    name: str | None = None
    phone: str | None = None
    status: str | None = None
    profile: str | None = None
    allowed_departments: str | None = None
    allowed_groups: str | None = None
    permissions: str | None = None
    notes: str | None = None


# ── Helpers ──────────────────────────────────────────────────────────

def _get_user(db: Session, church_id: int, user_id: int) -> AuthorizedUser:
    row = db.get(AuthorizedUser, user_id)
    if not row or row.church_id != church_id:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    return row


def _user_out(u: AuthorizedUser) -> dict:
    return {
        "id": u.id,
        "name": u.name,
        "phone": u.phone,
        "status": u.status,
        "profile": u.profile,
        "allowed_departments": u.allowed_departments,
        "allowed_groups": u.allowed_groups,
        "permissions": u.permissions,
        "notes": u.notes,
        "last_used_at": u.last_used_at.isoformat() if u.last_used_at else None,
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "effective_permissions": get_effective_permissions(u),
    }


# ── Rotas fixas ANTES de /{user_id} ─────────────────────────────────

@router.get("/profiles")
def list_profiles():
    return [{"id": k, "label": k.title(), "permissions": v} for k, v in PROFILE_DEFAULTS.items()]


@router.get("/permissions")
def list_permissions():
    return [{"id": p, "label": p.replace("_", " ").title()} for p in ALL_PERMISSIONS]


# ── CRUD ─────────────────────────────────────────────────────────────

@router.get("", response_model=list[dict])
def list_users(
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    return [
        _user_out(u)
        for u in db.query(AuthorizedUser)
        .filter(AuthorizedUser.church_id == church.id)
        .order_by(AuthorizedUser.name)
        .all()
    ]


@router.post("", response_model=dict, status_code=201)
def create_user(
    data: AuthUserCreate,
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    phone = "".join(c for c in data.phone if c.isdigit())
    if not phone or len(phone) < 10:
        raise HTTPException(status_code=400, detail="Telefone inválido")
    exists = (
        db.query(AuthorizedUser)
        .filter(AuthorizedUser.church_id == church.id, AuthorizedUser.phone == phone[:20])
        .first()
    )
    if exists:
        raise HTTPException(status_code=400, detail="Este telefone já está cadastrado")
    if data.profile not in PROFILE_DEFAULTS:
        raise HTTPException(status_code=400, detail="Perfil inválido")
    row = AuthorizedUser(
        church_id=church.id,
        name=data.name.strip(),
        phone=phone[:20],
        profile=data.profile,
        allowed_departments=data.allowed_departments,
        allowed_groups=data.allowed_groups,
        permissions=data.permissions,
        notes=data.notes,
        status="active",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _user_out(row)


@router.put("/{user_id}", response_model=dict)
def update_user(
    user_id: int,
    data: AuthUserUpdate,
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    row = _get_user(db, church.id, user_id)
    if data.name is not None:
        row.name = data.name.strip()
    if data.phone is not None:
        phone = "".join(c for c in data.phone if c.isdigit())
        if not phone or len(phone) < 10:
            raise HTTPException(status_code=400, detail="Telefone inválido")
        clash = (
            db.query(AuthorizedUser)
            .filter(
                AuthorizedUser.church_id == church.id,
                AuthorizedUser.phone == phone[:20],
                AuthorizedUser.id != row.id,
            )
            .first()
        )
        if clash:
            raise HTTPException(status_code=400, detail="Telefone já cadastrado")
        row.phone = phone[:20]
    if data.status is not None:
        row.status = data.status
    if data.profile is not None:
        if data.profile not in PROFILE_DEFAULTS:
            raise HTTPException(status_code=400, detail="Perfil inválido")
        row.profile = data.profile
    if data.allowed_departments is not None:
        row.allowed_departments = data.allowed_departments
    if data.allowed_groups is not None:
        row.allowed_groups = data.allowed_groups
    if data.permissions is not None:
        row.permissions = data.permissions
    if data.notes is not None:
        row.notes = data.notes
    db.commit()
    db.refresh(row)
    return _user_out(row)


@router.delete("/{user_id}", status_code=204)
def delete_user(
    user_id: int,
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    row = _get_user(db, church.id, user_id)
    db.delete(row)
    db.commit()


# ── Ações admin ──────────────────────────────────────────────────────

@router.get("/actions", response_model=list[dict])
def list_actions(
    limit: int = Query(default=50, ge=1, le=200),
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    return (
        db.query(AdminAction)
        .filter(AdminAction.church_id == church.id)
        .order_by(AdminAction.id.desc())
        .limit(limit)
        .all()
    )
