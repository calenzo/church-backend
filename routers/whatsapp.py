"""Endpoints da área de teste de envio do WhatsApp (aba Autorizados).

GET  /api/whatsapp/status        -> estado da conexão do número principal
GET  /api/whatsapp/grupos        -> lista REAL de grupos do WhatsApp
POST /api/whatsapp/enviar-grupo  -> envia uma mensagem REAL para um grupo
GET  /api/whatsapp/logs          -> histórico de envios (origem=teste)

O envio usa EXATAMENTE a mesma função dos comandos da IA (`execute_group_send`),
garantindo que teste e produção passem pelo mesmo caminho real.
"""

import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from auth import get_current_user
from config import settings
from database import get_db
from models import User, WhatsAppNumber
from services import evolution
from services.group_send import execute_group_send, list_send_logs
from routers.board import _connected_instance, _resolve_church

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])


class GroupSendIn(BaseModel):
    """Payload do teste de envio.

    Aceita camelCase (groupId/groupName — usado pelo painel) E snake_case
    (group_id/group_name), para não quebrar nenhum chamador existente.
    """
    model_config = ConfigDict(populate_by_name=True)

    group_id: str = Field(
        default="", max_length=120,
        validation_alias=AliasChoices("groupId", "group_id"),
    )
    group_name: str = Field(
        default="", max_length=160,
        validation_alias=AliasChoices("groupName", "group_name"),
    )
    message: str = Field(min_length=1, max_length=4000)


def _tz_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/status", response_model=dict)
async def whatsapp_status(
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    instance = await _connected_instance(db, church.id)
    if not instance:
        return {
            "connected": False,
            "state": "offline",
            "instance": "",
            "phone": "",
            "checked_at": _tz_now(),
        }
    try:
        state = await evolution.ping(instance, max_retries=1)
    except Exception:
        state = "offline"

    phone = ""
    if state == "open":
        phone = await _fetch_number_owner(instance)

    return {
        "connected": state == "open",
        "state": state,
        "instance": instance,
        "phone": phone,
        "checked_at": _tz_now(),
    }


async def _fetch_number_owner(instance: str) -> str:
    """Best-effort: busca o número conectado (ownerJid) na Evolution API."""
    try:
        base = settings.evolution_base_url.rstrip("/")
        headers = evolution._headers()

        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(f"{base}/instance/fetchInstances", headers=headers)
        if resp.status_code != 200:
            return ""
        data = resp.json()
        entries = data if isinstance(data, list) else data.get("data") or data.get("instances") or []
        for e in entries:
            inst = e.get("instance") if isinstance(e, dict) else {}
            if isinstance(inst, dict) and inst.get("instanceName") == instance:
                owner = inst.get("ownerJid") or inst.get("jid") or ""
                if owner:
                    return str(owner).split("@")[0]
        return ""
    except Exception:
        return ""


@router.get("/grupos", response_model=list[dict])
async def whatsapp_groups(
    refresh: bool = False,
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    instance = await _connected_instance(db, church.id)
    if not instance:
        raise HTTPException(status_code=400, detail="WhatsApp não conectado. Cadastre/conecte um número primeiro.")
    try:
        return await evolution.list_groups(instance=instance, force_refresh=refresh)
    except evolution.EvolutionError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/enviar-grupo", response_model=dict)
async def whatsapp_send_group(
    data: GroupSendIn,
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    instance = await _connected_instance(db, church.id)
    if not instance:
        raise HTTPException(status_code=400, detail="WhatsApp não conectado para esta igreja.")

    logger.info(
        "TESTE ENVIO RECEBIDO: {groupId=%r, groupName=%r, message=%r, church_id=%s, user=%s}",
        data.group_id, data.group_name, data.message[:300], church.id, user.name,
    )

    group_id = data.group_id.strip()
    group_name = data.group_name.strip()

    # Sem group_name e sem group_id: não há como enviar.
    if not group_name and not group_id:
        raise HTTPException(status_code=400, detail="Informe o grupo ou o nome do grupo.")

    # Prioridade: groupId informado usa direto. Sem groupId, procura pelo nome.
    if not group_id:
        from services.group_send import resolve_group_target

        hit = await resolve_group_target(db, church.id, group_name.strip(), instance=instance)
        if hit and hit.get("ambiguous"):
            raise HTTPException(
                status_code=404,
                detail="Encontrei grupos semelhantes: "
                + ", ".join(hit["names"])
                + ". Informe o nome exato do grupo.",
            )
        if not hit or not hit.get("group_id"):
            raise HTTPException(status_code=404, detail=f"Não encontrei o grupo {group_name}.")
        group_id = hit["group_id"]
        group_name = hit["name"]

    if not group_id.endswith("@g.us"):
        raise HTTPException(status_code=400, detail="Endereço de grupo inválido.")

    logger.info(
        "TESTE ENVIO ALVO: {groupId=%r, groupName=%r} — executando envio real",
        group_id, group_name,
    )

    outcome = await execute_group_send(
        db, church.id,
        group_id=group_id,
        group_name=group_name or group_id,
        message=data.message,
        instance=instance,
        user_name=user.name or "Painel",
        phone="",
        origin="painel",
    )
    return {
        "success": outcome["ok"],
        "status": outcome["status"],
        "groupId": outcome["group_id"],
        "groupName": outcome["group_name"],
        "messageId": outcome["message_id"],
        "timestamp": _tz_now(),
        "error": outcome["error"],
    }


@router.get("/logs", response_model=list[dict])
async def whatsapp_logs(
    origin: str = Query(default="painel"),
    limit: int = Query(default=30, ge=1, le=200),
    church_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    church = _resolve_church(db, user, church_id)
    return list_send_logs(db, church.id, origin=origin, limit=limit)