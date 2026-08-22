import re
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from auth import (
    create_session,
    destroy_session,
    get_current_user,
    hash_password,
    require_super_admin,
    verify_password,
)
from config import settings
from database import get_db
from models import Church, Department, LLMConfig, MessageLog, User, WhatsAppNumber
from routers.webhook import get_or_create_config
from schemas import (
    ChurchCreate,
    ChurchOut,
    ChurchUpdate,
    DepartmentCreate,
    DepartmentOut,
    DepartmentUpdate,
    LoginIn,
    LlmConfigIn,
    LlmConfigOut,
    MessageOut,
    NumberCreate,
    NumberOut,
    NumberUpdate,
    PairingCodeIn,
    ServiceStatus,
    TestSendIn,
    TokenOut,
    UserOut,
)
from services import evolution, llm
from services.evolution import EvolutionError
from services.llm import LlmError

router = APIRouter(prefix="/api", tags=["board"])


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "igreja"


def _resolve_church(db: Session, user: User, church_id: int | None) -> Church:
    """Super admin pode escolher qualquer igreja; admin fica preso à sua."""
    if user.role != "super_admin":
        if not user.church_id:
            raise HTTPException(status_code=403, detail="Usuário sem igreja vinculada")
        church = db.get(Church, user.church_id)
        if not church:
            raise HTTPException(status_code=404, detail="Igreja não encontrada")
        return church
    if church_id:
        church = db.get(Church, church_id)
        if not church:
            raise HTTPException(status_code=404, detail="Igreja não encontrada")
        return church
    church = db.query(Church).order_by(Church.id).first()
    if not church:
        raise HTTPException(status_code=404, detail="Nenhuma igreja cadastrada")
    return church


def _accessible_number(db: Session, user: User, number_id: int) -> WhatsAppNumber:
    number = db.get(WhatsAppNumber, number_id)
    if not number:
        raise HTTPException(status_code=404, detail="Número não encontrado")
    if user.role != "super_admin" and number.church_id != user.church_id:
        raise HTTPException(status_code=403, detail="Sem acesso a este número")
    return number


def _primary_instance(db: Session, church_id: int | None) -> str | None:
    number = (
        db.query(WhatsAppNumber)
        .filter(WhatsAppNumber.church_id == church_id, WhatsAppNumber.active == True)  # noqa: E712
        .order_by(WhatsAppNumber.id)
        .first()
    )
    return number.instance_name if number else None


# ----------------------------- Auth -----------------------------


@router.post("/auth/login", response_model=TokenOut)
def login(data: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == data.email.strip().lower()).first()
    if not user or not user.active or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="E-mail ou senha inválidos")
    session = create_session(db, user)
    return {"token": session.token, "user": user}


@router.get("/auth/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.post("/auth/logout")
def logout(
    x_auth_token: str | None = Header(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if x_auth_token:
        destroy_session(db, x_auth_token)
    return {"ok": True}


@router.post("/auth/password", response_model=dict)
def change_password(
    data: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    current = data.get("current") or ""
    new = data.get("next") or ""
    if len(new) < 6:
        raise HTTPException(status_code=400, detail="A nova senha deve ter ao menos 6 caracteres")
    if not verify_password(current, user.password_hash):
        raise HTTPException(status_code=400, detail="Senha atual incorreta")
    user.password_hash = hash_password(new)
    db.commit()
    return {"ok": True}


# ----------------------------- Igrejas (super admin) -----------------------------


@router.get("/churches", response_model=list[ChurchOut])
def list_churches(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role == "super_admin":
        return db.query(Church).order_by(Church.name).all()
    church = _resolve_church(db, user, None)
    return [church]


@router.post("/churches", response_model=ChurchOut, status_code=201)
def create_church(
    data: ChurchCreate,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    slug = _slugify(data.name)
    while db.query(Church).filter(Church.slug == slug).first():
        slug = f"{_slugify(data.name)}-{secrets.token_hex(2)}"
    church = Church(name=data.name.strip(), slug=slug)
    db.add(church)
    db.commit()
    db.refresh(church)
    get_or_create_config(db, church.id)
    return church


@router.put("/churches/{church_id}", response_model=ChurchOut)
def update_church(
    church_id: int,
    data: ChurchUpdate,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    church = db.get(Church, church_id)
    if not church:
        raise HTTPException(status_code=404, detail="Igreja não encontrada")
    if data.name is not None:
        church.name = data.name.strip()
    if data.active is not None:
        church.active = data.active
    db.commit()
    db.refresh(church)
    return church


@router.delete("/churches/{church_id}", status_code=204)
async def delete_church(
    church_id: int,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    church = db.get(Church, church_id)
    if not church:
        raise HTTPException(status_code=404, detail="Igreja não encontrada")
    linked_users = db.query(User).filter(User.church_id == church_id).count()
    if linked_users:
        raise HTTPException(status_code=400, detail="Remova ou desvincule os usuários desta igreja antes de excluí-la")
    for number in db.query(WhatsAppNumber).filter(WhatsAppNumber.church_id == church_id).all():
        try:
            await evolution.delete_instance(number.instance_name)
        except EvolutionError:
            pass
        db.delete(number)
    db.query(Department).filter(Department.church_id == church_id).delete()
    db.query(MessageLog).filter(MessageLog.church_id == church_id).delete()
    db.query(LLMConfig).filter(LLMConfig.church_id == church_id).delete()
    db.delete(church)
    db.commit()


# ----------------------------- Números WhatsApp -----------------------------


@router.get("/churches/{church_id}/numbers", response_model=list[NumberOut])
def list_numbers(
    church_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _resolve_church(db, user, church_id)
    return db.query(WhatsAppNumber).filter(WhatsAppNumber.church_id == church_id).order_by(WhatsAppNumber.id).all()


@router.post("/churches/{church_id}/numbers", response_model=NumberOut, status_code=201)
async def create_number(
    church_id: int,
    data: NumberCreate,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    instance = (data.instance_name or "").strip() or f"{church.slug}-{secrets.token_hex(3)}"
    if db.query(WhatsAppNumber).filter(WhatsAppNumber.instance_name == instance).first():
        raise HTTPException(status_code=400, detail="Já existe um número com esse nome de instância")
    await evolution.create_instance(instance)
    number = WhatsAppNumber(church_id=church.id, instance_name=instance, label=data.label.strip())
    db.add(number)
    db.commit()
    db.refresh(number)
    return number


@router.put("/numbers/{number_id}", response_model=NumberOut)
def update_number(
    number_id: int,
    data: NumberUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    number = _accessible_number(db, user, number_id)
    if data.label is not None:
        number.label = data.label.strip()
    if data.active is not None:
        number.active = data.active
    db.commit()
    db.refresh(number)
    return number


@router.delete("/numbers/{number_id}", status_code=204)
async def delete_number(
    number_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    number = _accessible_number(db, user, number_id)
    try:
        await evolution.delete_instance(number.instance_name)
    except EvolutionError:
        pass
    db.delete(number)
    db.commit()


@router.get("/numbers/{number_id}/state", response_model=dict)
async def number_state(
    number_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    number = _accessible_number(db, user, number_id)
    try:
        state = await evolution.ping(number.instance_name)
    except EvolutionError:
        state = "offline"
    return {"state": state}


@router.get("/numbers/{number_id}/qrcode", response_model=dict)
async def number_qrcode(
    number_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    number = _accessible_number(db, user, number_id)
    try:
        qr = await evolution.get_qrcode(number.instance_name)
        return {"ok": True, "qrcode": qr}
    except EvolutionError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/numbers/{number_id}/pairing-code", response_model=dict)
async def number_pairing_code(
    number_id: int,
    data: PairingCodeIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    number = _accessible_number(db, user, number_id)
    try:
        result = await evolution.get_pairing_code(data.number, number.instance_name)
        return {"ok": True, **result}
    except EvolutionError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/numbers/{number_id}/disconnect", response_model=dict)
async def number_disconnect(
    number_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    number = _accessible_number(db, user, number_id)
    try:
        result = await evolution.logout_instance(number.instance_name)
        return {"ok": True, **result}
    except EvolutionError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ----------------------------- Config LLM (por igreja) -----------------------------


@router.get("/config", response_model=LlmConfigOut)
def get_config(
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    return get_or_create_config(db, church.id)


@router.put("/config", response_model=LlmConfigOut)
def update_config(
    data: LlmConfigIn,
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    config = get_or_create_config(db, church.id)
    for field, value in data.model_dump().items():
        setattr(config, field, value)
    db.commit()
    db.refresh(config)
    return config


@router.post("/config/test", response_model=dict)
async def test_llm(
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    config = get_or_create_config(db, church.id)
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


# ----------------------------- Departamentos (por igreja) -----------------------------


@router.get("/departments", response_model=list[DepartmentOut])
def list_departments(
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    return db.query(Department).filter(Department.church_id == church.id).order_by(Department.name).all()


@router.post("/departments", response_model=DepartmentOut, status_code=201)
def create_department(
    data: DepartmentCreate,
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    dep = Department(**data.model_dump(), church_id=church.id)
    db.add(dep)
    db.commit()
    db.refresh(dep)
    return dep


@router.put("/departments/{dep_id}", response_model=DepartmentOut)
def update_department(
    dep_id: int,
    data: DepartmentUpdate,
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    dep = db.query(Department).filter(Department.id == dep_id, Department.church_id == church.id).first()
    if not dep:
        raise HTTPException(status_code=404, detail="Departamento não encontrado")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(dep, field, value)
    db.commit()
    db.refresh(dep)
    return dep


@router.delete("/departments/{dep_id}", status_code=204)
def delete_department(
    dep_id: int,
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    dep = db.query(Department).filter(Department.id == dep_id, Department.church_id == church.id).first()
    if not dep:
        raise HTTPException(status_code=404, detail="Departamento não encontrado")
    db.delete(dep)
    db.commit()


@router.post("/departments/{dep_id}/test", response_model=dict)
async def test_department(
    dep_id: int,
    data: TestSendIn,
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    dep = db.query(Department).filter(Department.id == dep_id, Department.church_id == church.id).first()
    if not dep:
        raise HTTPException(status_code=404, detail="Departamento não encontrado")
    number = data.number or dep.group_jid
    if not number:
        raise HTTPException(status_code=400, detail="Defina o JID do grupo no departamento")
    instance = _primary_instance(db, church.id)
    if not instance:
        raise HTTPException(status_code=400, detail="Cadastre um número do WhatsApp para esta igreja")
    try:
        await evolution.send_text(number, data.text, instance=instance)
        return {"ok": True}
    except EvolutionError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ----------------------------- Mensagens / Status / Grupos -----------------------------


@router.get("/messages", response_model=list[MessageOut])
def list_messages(
    limit: int = 100,
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    limit = max(1, min(limit, 500))
    return (
        db.query(MessageLog)
        .filter(MessageLog.church_id == church.id)
        .order_by(MessageLog.created_at.desc())
        .limit(limit)
        .all()
    )


@router.get("/status", response_model=ServiceStatus)
async def status(
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    config = get_or_create_config(db, church.id)
    instance = _primary_instance(db, church.id)
    result = ServiceStatus(
        llm="desconhecido",
        evolution="offline",
        llm_model=config.model,
        instance=instance or "",
    )
    try:
        result.llm = "ok" if await llm.ping(config.base_url, config.model, config.api_key) else "offline"
    except Exception:
        result.llm = "offline"
    if instance:
        try:
            state = await evolution.ping(instance)
            result.evolution = "ok" if state == "open" else (state or "offline")
        except Exception:
            result.evolution = "offline"
    return result


@router.get("/evolution/groups", response_model=list[dict])
async def evolution_groups(
    refresh: bool = False,
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    instance = _primary_instance(db, church.id)
    if not instance:
        raise HTTPException(status_code=400, detail="Cadastre um número do WhatsApp para esta igreja")
    try:
        return await evolution.list_groups(instance=instance, force_refresh=refresh)
    except EvolutionError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/evolution/debug")
async def evolution_debug(user: User = Depends(require_super_admin)):
    """Endpoint de diagnóstico para verificar a conexão com a Evolution API."""
    import httpx as _httpx

    results: dict = {}
    base = settings.evolution_base_url.rstrip("/")
    headers = evolution._headers()

    try:
        async with _httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{base}/instance/fetchInstances", headers=headers)
            results["fetchInstances"] = {"status": r.status_code, "body": r.json() if r.status_code == 200 else r.text[:500]}
    except Exception as e:
        results["fetchInstances"] = {"error": str(e)}

    return results


@router.get("/qr")
async def qr_page():
    """Página HTML com o QR code da instância padrão (sem login, uso manual)."""
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
