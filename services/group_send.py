"""Envio real de mensagens para grupos do WhatsApp.

Regra crítica: nenhuma mensagem é considerada 'enviada' apenas por
interpretação da IA. Só o retorno real da Evolution/WhatsApp marca o
status 'enviado'. Cada tentativa fica em `group_send_logs` com status:

  pendente -> enviando -> enviado (com messageId do WhatsApp)
                       -> erro (com motivo técnico)

Usado tanto pelos comandos de usuário autorizado (via conversa) quanto
pela área de teste da aba Autorizados — mesma função real de envio.
"""

import json
import logging

from sqlalchemy.orm import Session

from models import Department, GroupSendLog
from services import evolution

logger = logging.getLogger(__name__)


def _extract_message_id(resp: dict | None) -> str:
    """Tenta extrair o messageId da resposta da Evolution (formatos variados)."""
    if not isinstance(resp, dict):
        return ""
    key = resp.get("key")
    if isinstance(key, dict) and key.get("id"):
        return str(key["id"])
    return str(resp.get("messageId") or resp.get("id") or resp.get("keyId") or "")


def create_send_log(
    db: Session,
    church_id: int,
    *,
    user_name: str,
    phone: str,
    group_name: str,
    group_id: str,
    message: str,
    origin: str = "ia",
) -> GroupSendLog:
    row = GroupSendLog(
        church_id=church_id,
        user_name=user_name[:120],
        phone=phone[:20],
        group_name=group_name[:160],
        group_id=group_id[:120],
        message=message,
        status="pendente",
        origin="ia" if origin not in ("teste", "ia") else origin,
    )
    db.add(row)
    db.flush()
    return row


def get_last_send_log(db: Session, church_id: int, phone: str = "", origin: str = "") -> GroupSendLog | None:
    query = db.query(GroupSendLog).filter(GroupSendLog.church_id == church_id)
    if phone:
        query = query.filter(GroupSendLog.phone == phone)
    if origin:
        query = query.filter(GroupSendLog.origin == origin)
    return query.order_by(GroupSendLog.id.desc()).first()


def list_send_logs(db: Session, church_id: int, origin: str = "", limit: int = 50) -> list[dict]:
    query = db.query(GroupSendLog).filter(GroupSendLog.church_id == church_id)
    if origin:
        query = query.filter(GroupSendLog.origin == origin)
    rows = query.order_by(GroupSendLog.id.desc()).limit(max(1, min(limit, 200))).all()
    return [
        {
            "id": r.id,
            "user_name": r.user_name,
            "phone": r.phone,
            "group_name": r.group_name,
            "group_id": r.group_id,
            "message": r.message,
            "status": r.status,
            "message_id": r.message_id,
            "error": r.error,
            "origin": r.origin,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


def _norm(value: str) -> str:
    import unicodedata

    return "".join(
        ch
        for ch in unicodedata.normalize("NFD", (value or "").lower())
        if unicodedata.category(ch) != "Mn"
    )


async def resolve_group_target(
    db: Session,
    church_id: int,
    group_name: str,
    instance: str | None = None,
) -> dict | None:
    """Localiza o grupo WhatsApp real pelo nome.

    1. Tenta o vínculo cadastrado no departamento (nome ou grupo vinculado);
    2. Senão, consulta a lista REAL de grupos do WhatsApp e casa pelo nome.
    Retorna {"name": ..., "group_id": ...} ou None se não encontrar.
    NUNCA inventa um vínculo: se não encontrar, retorna None."""
    wanted = _norm(group_name)
    if not wanted:
        return None

    depts = (
        db.query(Department)
        .filter(Department.church_id == church_id, Department.active == True)  # noqa: E712
        .all()
    )
    for d in depts:
        candidates = []
        if (d.group_name or "").strip():
            candidates.append(d.group_name)
        candidates.append(d.name)
        for cand in candidates:
            if _norm(cand) == wanted:
                if d.group_jid:
                    return {"name": d.group_name or d.name, "group_id": d.group_jid, "source": "departamento"}
                break
        else:
            continue
        break

    # Busca na lista REAL de grupos do WhatsApp.
    try:
        groups = await evolution.list_groups(instance=instance, force_refresh=False)
    except Exception:
        groups = []
    for g in groups:
        subject = (g.get("subject") or "").strip()
        if subject and _norm(subject) == wanted:
            return {"name": subject, "group_id": g.get("id") or "", "source": "whatsapp"}
    return None


async def execute_group_send(
    db: Session,
    church_id: int,
    *,
    group_id: str,
    group_name: str,
    message: str,
    instance: str,
    user_name: str,
    phone: str,
    origin: str = "ia",
    dedup_key: str = "",
) -> dict:
    """Executa o envio REAL para o grupo, registrando o status.

    Retorna {"ok": bool, "status": str, "message_id": str, "error": str, "log_id": int, "group_name": str}
    """
    send_log = create_send_log(
        db, church_id,
        user_name=user_name, phone=phone, group_name=group_name,
        group_id=group_id, message=message, origin=origin,
    )
    send_log.status = "enviando"
    db.commit()

    try:
        from services.safety import safe_send

        result = await safe_send(
            church_id,
            group_id,
            message,
            instance=instance,
            message_id=dedup_key or f"grpsend:{send_log.id}",
            is_reply=False,
        )
        if result.get("ok"):
            msg_id = _extract_message_id(result.get("response"))
            send_log.status = "enviado"
            send_log.message_id = msg_id[:120]
            db.commit()
            logger.info(
                "[COMMAND] enviar_mensagem_grupo [RESULT] sucesso [GROUP_ID] %s "
                "[MESSAGE_ID] %s [MESSAGE] %s",
                group_id, msg_id, message,
            )
            return {
                "ok": True,
                "status": "enviado",
                "message_id": msg_id,
                "error": "",
                "log_id": send_log.id,
                "group_name": group_name,
                "group_id": group_id,
            }
        status = result.get("status", "erro")
        detail = result.get("detail", "")
        send_log.status = "erro"
        send_log.error = (f"{status}: {detail}" if detail else status)[:500]
        db.commit()
        return {
            "ok": False,
            "status": status,
            "message_id": "",
            "error": send_log.error,
            "log_id": send_log.id,
            "group_name": group_name,
            "group_id": group_id,
        }
    except evolution.EvolutionError as exc:
        send_log.status = "erro"
        send_log.error = str(exc)[:500]
        db.commit()
        logger.error(
            "[COMMAND] enviar_mensagem_grupo [RESULT] erro [GROUP_ID] %s [ERROR] %s",
            group_id, exc,
        )
        return {
            "ok": False,
            "status": "erro",
            "message_id": "",
            "error": str(exc),
            "log_id": send_log.id,
            "group_name": group_name,
            "group_id": group_id,
        }


def serialize_send_log(row: GroupSendLog) -> dict:
    return {
        "id": row.id,
        "user_name": row.user_name,
        "phone": row.phone,
        "group_name": row.group_name,
        "group_id": row.group_id,
        "message": row.message,
        "status": row.status,
        "message_id": row.message_id,
        "error": row.error,
        "origin": row.origin,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }