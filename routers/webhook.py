import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models import Department, LLMConfig, MessageLog
from services import evolution, llm

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])


def get_or_create_config(db: Session) -> LLMConfig:
    config = db.get(LLMConfig, 1)
    if config is None:
        config = LLMConfig(
            id=1,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            temperature=settings.llm_temperature,
        )
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


def _extract_text(data: dict) -> str:
    msg = data.get("message") or {}
    if isinstance(msg, dict):
        for key in ("conversation",):
            if msg.get(key):
                return msg[key]
        if msg.get("extendedTextMessage", {}).get("text"):
            return msg["extendedTextMessage"]["text"]
        for key in ("imageMessage", "videoMessage", "documentMessage"):
            if msg.get(key, {}).get("caption"):
                return msg[key]["caption"]
    return (data.get("body") or "").strip()


def _is_audio(data: dict) -> bool:
    msg = data.get("message") or {}
    if not isinstance(msg, dict):
        return False
    return bool(msg.get("audioMessage") or msg.get("ptvMessage") or msg.get("voiceMessage"))


def _normalize_jid(jid: str) -> str:
    return jid.split("@")[0] if "@" in jid else jid


def _verify_token(token: str | None):
    if settings.webhook_token and token != settings.webhook_token:
        raise HTTPException(status_code=401, detail="Token inválido")


def _add_step(log: MessageLog, step: str, status: str = "ok", detail: str = ""):
    """Registra um passo do fluxo no campo steps (JSON) para o dashboard."""
    try:
        steps = json.loads(log.steps) if log.steps else []
        if not isinstance(steps, list):
            steps = []
    except (TypeError, ValueError):
        steps = []
    steps.append({
        "step": step,
        "status": status,
        "detail": detail,
        "ts": datetime.utcnow().isoformat(),
    })
    log.steps = json.dumps(steps, ensure_ascii=False)


@router.post("/evolution")
async def evolution_webhook(request: Request, x_token: str | None = Header(default=None), db: Session = Depends(get_db)):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON inválido")

    event = payload.get("event") or payload.get("data", {}).get("event")
    data = payload.get("data") or payload

    # Só processamos mensagens novas
    if event not in ("messages.upsert", None):
        return {"ok": True, "skipped": event}

    key = data.get("key") or {}
    remote_jid = key.get("remoteJid") or data.get("remoteJid") or ""
    from_me = bool(key.get("fromMe") or data.get("fromMe"))

    # Ignora mensagens enviadas por nós e mensagens de grupos
    if from_me or not remote_jid.endswith("@s.whatsapp.net"):
        return {"ok": True, "skipped": "not_private_or_from_me"}

    config = get_or_create_config(db)

    text = _extract_text(data)
    media_key = None
    if _is_audio(data):
        text = ""
        msg = data.get("message") or {}
        audio = msg.get("audioMessage") or msg.get("ptvMessage") or msg.get("voiceMessage") or {}
        media_key = audio.get("mediaKey") or key.get("id")

    if not text and not media_key:
        return {"ok": True, "skipped": "no_text"}

    from_number = _normalize_jid(remote_jid)

    config = get_or_create_config(db)
    departments = [
        {
            "name": d.name,
            "description": d.description,
            "group_jid": d.group_jid,
        }
        for d in db.query(Department).filter(Department.active == True).all()
    ]

    log = MessageLog(
        direction="in",
        from_number=from_number,
        to_jid=settings.evolution_instance,
        text=text,
        status="received",
    )
    db.add(log)
    db.commit()
    db.refresh(log)

    _add_step(log, "mensagem recebida")
    db.commit()
    logger.info("Mensagem recebida de %s: %s", from_number, text[:80])

    try:
        if media_key:
            _add_step(log, "baixando mídia (áudio)")
            db.commit()
            audio_b64 = await evolution.get_media_base64(key.get("id"))
            if not audio_b64:
                raise llm.LlmError("Mídia não encontrada para transcrever")
            _add_step(log, "transcrevendo áudio")
            db.commit()
            text = await llm.transcribe_audio(audio_b64, config)
            log.text = text
            _add_step(log, "áudio transcrito", detail=text[:80])
            db.commit()

        _add_step(log, "classificando com a LLM")
        db.commit()
        result = await llm.classify_and_reply(text, departments, config)
    except (evolution.EvolutionError, llm.LlmError) as exc:
        log.status = "failed"
        log.error = str(exc)
        _add_step(log, "falha", status="error", detail=str(exc))
        db.commit()
        return {"ok": True, "error": str(exc)}
    except Exception as exc:  # nunca responder 500: registra e encerra
        logger.exception("Erro inesperado ao processar mensagem de %s", from_number)
        log.status = "failed"
        log.error = str(exc)
        _add_step(log, "erro inesperado", status="error", detail=str(exc))
        db.commit()
        return {"ok": True, "error": str(exc)}

    department_name = result["department"]
    reply = result["reply"]
    matched = next((d for d in departments if d["name"].lower() == department_name.lower()), None)
    matched_dep = None
    if matched:
        matched_dep = db.query(Department).filter(Department.name == matched["name"]).first()

    log.department_name = department_name
    log.department_id = matched_dep.id if matched_dep else None
    log.llm_reply = reply
    log.status = "routed"
    _add_step(log, "classificado", detail=f"departamento: {department_name}")
    db.commit()

    # 1) Encaminha a mensagem para o grupo do departamento, se houver grupo configurado
    if matched and matched.get("group_jid"):
        group_text = (
            f"NOVA MENSAGEM PARA {matched['name'].upper()}\n"
            f"De: {from_number}\n\n{text}"
        )
        try:
            await evolution.send_text(matched["group_jid"], group_text)
            _add_step(log, "encaminhado para o grupo", detail=matched["group_jid"])
        except evolution.EvolutionError as exc:
            logger.error("Falha ao encaminhar para o grupo %s: %s", matched["group_jid"], exc)
            log.error = log.error or ""
            log.error += f" | grupo: {exc}"
            log.status = "routed_with_error"
            _add_step(log, "falha ao encaminhar ao grupo", status="error", detail=str(exc))

    # 2) Envia a resposta da LLM de volta para o membro
    if reply:
        try:
            await evolution.send_text(f"{from_number}@s.whatsapp.net", reply)
            _add_step(log, "resposta enviada ao membro")
        except evolution.EvolutionError as exc:
            logger.error("Falha ao responder %s: %s", from_number, exc)
            log.error = log.error or ""
            log.error += f" | resposta: {exc}"
            log.status = "routed_with_error"
            _add_step(log, "falha ao enviar resposta", status="error", detail=str(exc))

    db.commit()
    return {"ok": True, "department": department_name}
