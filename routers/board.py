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
    PairingCodeIn,
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
async def evolution_groups(refresh: bool = False):
    try:
        return await evolution.list_groups(force_refresh=refresh)
    except EvolutionError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/evolution/debug")
async def evolution_debug():
    """Endpoint de diagnóstico para verificar a conexão com a Evolution API."""
    import httpx as _httpx
    results: dict = {}
    base = settings.evolution_base_url.rstrip("/")
    inst = settings.evolution_instance
    headers = evolution._headers()

    # 1. Ping / connectionState
    try:
        async with _httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{base}/instance/connectionState/{inst}", headers=headers)
            results["connectionState"] = {"status": r.status_code, "body": r.json()}
    except Exception as e:
        results["connectionState"] = {"error": str(e)}

    # 2. fetchAllGroups (raw)
    try:
        async with _httpx.AsyncClient(timeout=30) as c:
            r = await c.get(
                f"{base}/group/fetchAllGroups/{inst}",
                params={"getParticipants": "false"},
                headers=headers,
            )
            results["fetchAllGroups"] = {"status": r.status_code, "body": r.json() if r.status_code == 200 else r.text[:500]}
    except Exception as e:
        results["fetchAllGroups"] = {"error": str(e)}

    # 3. Informações da instância
    try:
        async with _httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{base}/instance/fetchInstances", headers=headers)
            results["fetchInstances"] = {"status": r.status_code, "body": r.json() if r.status_code == 200 else r.text[:500]}
    except Exception as e:
        results["fetchInstances"] = {"error": str(e)}

    return results


@router.get("/evolution/qrcode", response_model=dict)
async def evolution_qrcode():
    try:
        qr = await evolution.get_qrcode()
        return {"ok": True, "qrcode": qr}
    except EvolutionError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/evolution/pairing-code", response_model=dict)
async def evolution_pairing_code(data: PairingCodeIn):
    """Gera um código de pareamento numérico para conectar sem QR code.
    O número deve estar no formato 5511999999999."""
    try:
        code = await evolution.get_pairing_code(data.number)
        return {"ok": True, "pairingCode": code}
    except EvolutionError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/qr")
async def qr_page():
    """Página HTML com o QR code para escanear com o celular."""
    from fastapi.responses import HTMLResponse

    try:
        qr_b64 = await evolution.get_qrcode()
    except EvolutionError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    if not qr_b64:
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;text-align:center;padding:40px'>"
            "<h2>WhatsApp já conectado</h2>"
            "<p>A instância já está ativa. Não é necessário escanear QR code.</p>"
            "</body></html>"
        )

    img_src = qr_b64 if qr_b64.startswith("data:") else f"data:image/png;base64,{qr_b64}"

    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>WhatsApp QR Code</title>
        <style>
            body {{ font-family: -apple-system, sans-serif; text-align: center; padding: 20px; background: #f5f5f5; }}
            .card {{ background: white; border-radius: 12px; padding: 24px; max-width: 400px; margin: 20px auto; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
            img {{ max-width: 100%; border-radius: 8px; }}
            h2 {{ color: #333; margin-bottom: 8px; }}
            p {{ color: #666; font-size: 14px; }}
            .step {{ text-align: left; background: #f0f7ff; padding: 12px 16px; border-radius: 8px; margin-top: 16px; font-size: 13px; color: #333; }}
            .step strong {{ color: #075e54; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h2>Conectar WhatsApp</h2>
            <p>Escaneie o QR Code abaixo com seu celular</p>
            <img src="{img_src}" alt="QR Code WhatsApp" />
            <div class="step">
                <strong>Como escanear:</strong><br>
                1. Abra o WhatsApp no celular<br>
                2. Vá em <em>Aparelhos conectados</em><br>
                3. Toque em <em>Conectar aparelho</em><br>
                4. Aponte a câmera para o QR Code acima
            </div>
        </div>
    </body>
    </html>
    """)


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
