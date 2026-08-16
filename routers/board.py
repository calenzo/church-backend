from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models import Department, MessageLog
from routers.webhook import get_or_create_config
from schemas import (
    DepartmentCreate,
    DepartmentOut,
    DepartmentUpdate,
    LlmConfigIn,
    LlmConfigOut,
    MessageOut,
    ServiceStatus,
    TestSendIn,
)
from services import evolution, llm
from services.evolution import EvolutionError
from services.llm import LlmError

router = APIRouter(prefix="/api", tags=["board"])


@router.get("/config", response_model=LlmConfigOut)
def get_config(db: Session = Depends(get_db)):
    return get_or_create_config(db)


@router.put("/config", response_model=LlmConfigOut)
def update_config(data: LlmConfigIn, db: Session = Depends(get_db)):
    config = get_or_create_config(db)
    for field, value in data.model_dump().items():
        setattr(config, field, value)
    db.commit()
    db.refresh(config)
    return config


@router.post("/config/test", response_model=dict)
async def test_llm(db: Session = Depends(get_db)):
    config = get_or_create_config(db)
    try:
        version = await llm.ping(config.base_url, config.model, config.api_key)
        result = await llm.classify_and_reply(
            "Olá, eu gostaria de saber o horário dos cultos de domingo.",
            [{"name": "geral", "description": "assuntos gerais"}],
            config,
        )
        return {"ok": True, "version": version, "classification": result}
    except LlmError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/departments", response_model=list[DepartmentOut])
def list_departments(db: Session = Depends(get_db)):
    return db.query(Department).order_by(Department.name).all()


@router.post("/departments", response_model=DepartmentOut, status_code=201)
def create_department(data: DepartmentCreate, db: Session = Depends(get_db)):
    dep = Department(**data.model_dump())
    db.add(dep)
    db.commit()
    db.refresh(dep)
    return dep


@router.put("/departments/{dep_id}", response_model=DepartmentOut)
def update_department(dep_id: int, data: DepartmentUpdate, db: Session = Depends(get_db)):
    dep = db.get(Department, dep_id)
    if not dep:
        raise HTTPException(status_code=404, detail="Departamento não encontrado")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(dep, field, value)
    db.commit()
    db.refresh(dep)
    return dep


@router.delete("/departments/{dep_id}", status_code=204)
def delete_department(dep_id: int, db: Session = Depends(get_db)):
    dep = db.get(Department, dep_id)
    if not dep:
        raise HTTPException(status_code=404, detail="Departamento não encontrado")
    db.delete(dep)
    db.commit()


@router.post("/departments/{dep_id}/test", response_model=dict)
async def test_department(dep_id: int, data: TestSendIn, db: Session = Depends(get_db)):
    dep = db.get(Department, dep_id)
    if not dep:
        raise HTTPException(status_code=404, detail="Departamento não encontrado")
    number = data.number or dep.group_jid
    if not number:
        raise HTTPException(status_code=400, detail="Defina o JID do grupo no departamento")
    try:
        await evolution.send_text(number, data.text)
        return {"ok": True}
    except EvolutionError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/messages", response_model=list[MessageOut])
def list_messages(limit: int = 100, db: Session = Depends(get_db)):
    limit = max(1, min(limit, 500))
    return db.query(MessageLog).order_by(MessageLog.created_at.desc()).limit(limit).all()


@router.get("/evolution/groups", response_model=list[dict])
async def evolution_groups():
    try:
        return await evolution.list_groups()
    except EvolutionError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/evolution/qrcode", response_model=dict)
async def evolution_qrcode():
    try:
        qr = await evolution.get_qrcode()
        return {"ok": True, "qrcode": qr}
    except EvolutionError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/status", response_model=ServiceStatus)
async def status(db: Session = Depends(get_db)):
    config = get_or_create_config(db)
    result = ServiceStatus(
        llm="desconhecido",
        evolution="desconhecido",
        llm_model=config.model,
        instance=settings.evolution_instance,
    )
    try:
        result.llm = "ok" if await llm.ping(config.base_url, config.model, config.api_key) else "offline"
    except Exception:
        result.llm = "offline"
    try:
        state = await evolution.ping()
        result.evolution = "ok" if state == "open" else (state or "offline")
    except Exception:
        result.evolution = "offline"
    return result
