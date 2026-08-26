"""Endpoints da aba Segurança WhatsApp: status, métricas, pausa/retoma."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import SafetyLog, User
from routers.board import _resolve_church
from services.safety import (
    ProtectionMode,
    get_church_safety,
)

router = APIRouter(prefix="/api/safety", tags=["safety"])


class SafetyLimitsIn(BaseModel):
    min_delay_sec: float | None = Field(default=None, ge=1, le=60)
    max_delay_sec: float | None = Field(default=None, ge=1, le=120)
    max_per_minute: int | None = Field(default=None, ge=1, le=100)
    max_per_hour: int | None = Field(default=None, ge=1, le=1000)
    cooldown_sec: float | None = Field(default=None, ge=0, le=60)
    debounce_sec: float | None = Field(default=None, ge=0, le=30)


@router.get("")
def get_status(
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    safety = get_church_safety(church.id)
    return safety.snapshot()


@router.post("/pausar")
def pause(
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    safety = get_church_safety(church.id)
    safety.set_paused(True)
    db.add(SafetyLog(church_id=church.id, event_type="pausa", detail="Pausado pelo admin"))
    db.commit()
    return {"ok": True, "mode": "PAUSADO"}


@router.post("/retomar")
def resume(
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    safety = get_church_safety(church.id)
    safety.set_paused(False)
    db.add(SafetyLog(church_id=church.id, event_type="retomada", detail="Retomado pelo admin"))
    db.commit()
    return {"ok": True, "mode": "NORMAL"}


@router.put("/limites")
def update_limits(
    data: SafetyLimitsIn,
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    safety = get_church_safety(church.id)
    kwargs = {k: v for k, v in data.model_dump().items() if v is not None}
    safety.update_limits(**kwargs)
    db.add(SafetyLog(
        church_id=church.id,
        event_type="limites",
        detail=f"Limites atualizados: {kwargs}",
    ))
    db.commit()
    return safety.snapshot()


@router.get("/logs")
def list_logs(
    limit: int = 50,
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    return (
        db.query(SafetyLog)
        .filter(SafetyLog.church_id == church.id)
        .order_by(SafetyLog.id.desc())
        .limit(min(limit, 200))
        .all()
    )
