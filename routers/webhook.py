import asyncio
import json
import logging
import re
import time
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from config import settings
from database import SessionLocal, get_db
from models import Church, Contact, Department, LLMConfig, MessageLog, WhatsAppNumber
from services import evolution, llm

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])

# IDs de mensagens recentes para evitar reprocessamento em retries da Evolution
_recent = {}


def _dedup(message_id: str, ttl: int = 20) -> bool:
    if not message_id:
        return False
    now = time.monotonic()
    if now - _recent.get(message_id, -ttl) < ttl:
        return True
    _recent[message_id] = now
    return False


def get_or_create_config(db: Session, church_id: int | None = None) -> LLMConfig:
    config = db.query(LLMConfig).filter(LLMConfig.church_id == church_id).first()
    if config is None:
        config = LLMConfig(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            temperature=settings.llm_temperature,
            church_id=church_id,
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


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


# Mapeamento aprendido em tempo real: JID @lid -> número real (só dígitos).
# O WhatsApp novo identifica participantes de grupo por LID, que não é telefone.
_lid_map: dict[str, str] = {}
# Nome informado pelo próprio remetente quando o número ainda não foi resolvido
# (LID sem pareamento): vale enquanto o processo estiver de pé.
_lid_name_map: dict[str, dict[str, str]] = {}


def _remember_lid(key: dict) -> None:
    """Sempre que a Evolution mandar o par (participant=@lid, participantPn=telefone),
    guarda a correspondência para reconhecer o contato nas próximas mensagens."""
    pn = key.get("participantPn") or key.get("participantPhoneNumber") or ""
    lid = key.get("participant") or ""
    if isinstance(pn, str) and pn.endswith("@s.whatsapp.net") and lid.endswith("@lid"):
        _lid_map[_normalize_jid(lid)] = _normalize_jid(pn)


async def _learn_group_lids(instance_name: str, group_jid: str) -> tuple[int, int]:
    """Busca os participantes do grupo na Evolution e aprende @lid -> telefone.
    Retorna (total de participantes, lids mapeados com sucesso)."""
    try:
        participants = await evolution.fetch_group_participants(instance_name, group_jid)
    except Exception as exc:
        logger.warning("Não foi possível buscar participantes de %s: %s", group_jid, exc)
        return 0, 0
    learned = 0
    for p in participants:
        pid = p.get("id") or p.get("jid") or p.get("participant") or ""
        phone = (
            p.get("phoneNumber")
            or p.get("participantPhoneNumber")
            or p.get("pn")
            or p.get("phone")
            or ""
        )
        if isinstance(pid, str) and pid.endswith("@lid") and phone:
            digits = _normalize_jid(str(phone))
            if digits:
                _lid_map[_normalize_jid(pid)] = digits
                learned += 1
    logger.info(
        "Grupo %s: %d participantes, %d LIDs mapeados", group_jid, len(participants), learned
    )
    return len(participants), learned


def _sender_phone(data: dict, key: dict, is_group: bool) -> str:
    """Melhor número real do remetente. Preferência: participantPhoneNumber/
    participantPn -> senderPn -> mapa @lid->telefone -> JID."""
    for cand in (
        key.get("participantPhoneNumber"),
        key.get("participantPn"),
        data.get("senderPn"),
    ):
        if cand and isinstance(cand, str):
            digits = _normalize_jid(cand)
            if digits:
                return digits
    jid = (key.get("participant") if is_group else key.get("remoteJid")) or ""
    if jid.endswith("@lid"):
        mapped = _lid_map.get(_normalize_jid(jid))
        if mapped:
            return mapped
    return _normalize_jid(jid)


def _lookup_contact(db: Session, church_id: int | None, phone: str) -> Contact | None:
    """Encontra o contato cadastrado pelo número do remetente (só dígitos).
    Tolerante ao DDI 55: cadastro '219999069940' casa com JID '55219999069940'.
    Prefere contatos com nome; linhas em branco (marcadores) ficam por último.
    Para LIDs ainda não pareados, usa o nome que o próprio remetente informou."""
    digits = _digits(phone)
    if not digits or not church_id:
        return None
    candidates = [digits]
    if digits.startswith("55") and len(digits) >= 12:
        candidates.append(digits[2:])
    rows = (
        db.query(Contact)
        .filter(Contact.church_id == church_id, Contact.phone.in_(candidates))
        .all()
    )
    for row in rows:
        if (row.name or "").strip():
            return row
    remembered = _lid_name_map.get(digits)
    if remembered:
        return Contact(
            church_id=church_id,
            phone=digits,
            name=remembered.get("name", ""),
            role=remembered.get("role", ""),
        )
    if rows:
        return rows[0]
    return None


# Frases de apresentação: "meu nome é X", "sou o Y", "aqui é a Z" etc.
_SELF_NAME_RE = re.compile(
    r"(?:meu nome (?:é|e)|me chamo|aqui (?:é|e)\s+[oa]|eu\s+sou\s+[oa]|sou\s+[oa])\s+"
    r"([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ']*(?:\s+[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ']+){0,3})",
    re.IGNORECASE,
)
_ROLE_WORDS = {
    "irmã", "irma", "irmão", "irmao", "missionária", "missionaria", "diaconisa",
    "diácono", "diacono", "presbítero", "presbitero", "evangelista",
    "pastor", "pastora", "reverendo",
}


def _extract_self_name(text: str) -> tuple[str, str]:
    """Extrai (nome, cargo) de frases de apresentação, sem depender da LLM."""
    match = _SELF_NAME_RE.search(text or "")
    if not match:
        return "", ""
    words = match.group(1).strip().strip("!,.?;:").split()
    role = ""
    name_words = []
    for word in words:
        clean = word.strip(".,")
        if not name_words and clean.lower() in _ROLE_WORDS:
            role = clean.title()
        else:
            name_words.append(clean)
    name = " ".join(name_words)
    if not (2 <= len(name) <= 60) or not re.search(r"[A-Za-zÀ-ÿ]", name):
        return "", ""
    return name, role


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


def _load_history(db: Session, from_number: str, church_id: int | None, exclude_id: int, limit: int = 8) -> list[dict]:
    """Carrega as últimas mensagens trocadas com este contato para dar contexto à LLM."""
    rows = (
        db.query(MessageLog)
        .filter(
            MessageLog.church_id == church_id,
            MessageLog.from_number == from_number,
            MessageLog.id != exclude_id,
            MessageLog.text != "",
        )
        .order_by(MessageLog.created_at.desc(), MessageLog.id.desc())
        .limit(limit)
        .all()
    )
    history: list[dict] = []
    for row in reversed(rows):
        entry = {"member": row.text}
        if row.llm_reply:
            entry["assistant"] = row.llm_reply
        history.append(entry)
    return history


async def _process_message(log_id: int, instance_name: str):
    """Processa a mensagem em segundo plano, atualizando o log (steps) a cada etapa."""
    db = SessionLocal()
    try:
        log = db.get(MessageLog, log_id)
        if log is None:
            return
        config = get_or_create_config(db, log.church_id)
        departments = [
            {
                "name": d.name,
                "description": d.description,
                "group_jid": d.group_jid,
            }
            for d in db.query(Department)
            .filter(Department.active == True, Department.church_id == log.church_id)  # noqa: E712
            .all()
        ]
        text = log.text or ""
        from_number = log.from_number
        media_key = log.media_key or None

        # Mensagem de grupo: a conversa acontece no grupo de um departamento;
        # restringe o contexto ao(s) departamento(s) daquele grupo e responde nele.
        reply_in_group = bool(log.to_jid and log.to_jid.endswith("@g.us"))
        scope_departments = departments
        if reply_in_group:
            linked = [d for d in departments if d["group_jid"] == log.to_jid]
            if linked:
                scope_departments = linked

        try:
            if media_key:
                _add_step(log, "baixando mídia (áudio)")
                db.commit()
                audio_b64 = await evolution.get_media_base64(
                    log.media_message_id or media_key,
                    remote_jid=log.to_jid,
                    instance=instance_name,
                )
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
            history = _load_history(db, from_number, log.church_id, exclude_id=log.id)
            contact = _lookup_contact(db, log.church_id, from_number)
            known = bool(contact and (contact.name or "").strip())
            if known:
                detail = contact.name + (f" ({contact.role})" if contact.role else "")
                _add_step(log, "contato reconhecido", detail=detail)
            sender = {"name": contact.name, "role": contact.role} if known else None
            # Contato com linha em branco no cadastro = já convidamos a se identificar antes.
            asked_before = contact is not None and not known
            known_names = None
            if not known:
                known_names = [
                    c.name
                    for c in db.query(Contact).filter(Contact.church_id == log.church_id).all()
                    if (c.name or "").strip()
                ]
            result = await llm.classify_and_reply(
                text,
                scope_departments,
                config,
                history=history,
                sender=sender,
                asked_name_before=asked_before,
                known_names=known_names,
            )
        except (evolution.EvolutionError, llm.LlmError) as exc:
            log.status = "failed"
            log.error = str(exc)
            _add_step(log, "falha", status="error", detail=str(exc))
            db.commit()
            return
        except Exception as exc:
            logger.exception("Erro inesperado ao processar mensagem %s de %s", log_id, from_number)
            log.status = "failed"
            log.error = str(exc)
            _add_step(log, "erro inesperado", status="error", detail=str(exc))
            db.commit()
            return

        department_name = result["department"]
        reply = result["reply"]

        # Cadastro automático: nome dito pelo remetente (LLM) ou extraído da frase.
        new_name = (result.get("new_contact_name") or "").strip()
        new_role = (result.get("new_contact_role") or "").strip()
        if not new_name and not known:
            fb_name, fb_role = _extract_self_name(text)
            if fb_name:
                new_name, new_role = fb_name, (new_role or fb_role)
        phone_ok = bool(
            log.church_id and from_number.isdigit() and 10 <= len(from_number) <= 13
        )  # telefones reais; LIDs têm ~15+ dígitos
        phone_store = (
            from_number[2:]
            if from_number.startswith("55") and 12 <= len(from_number) <= 13
            else from_number
        )

        if new_name and phone_ok and re.search(r"[A-Za-zÀ-ÿ]", new_name):
            row = _lookup_contact(db, log.church_id, from_number)
            try:
                if row:
                    row.name = new_name[:160]
                    if new_role:
                        row.role = new_role[:80]
                else:
                    db.add(
                        Contact(
                            church_id=log.church_id,
                            phone=phone_store,
                            name=new_name[:160],
                            role=new_role[:80],
                        )
                    )
                db.commit()
                _add_step(
                    log,
                    "contato salvo pelo whatsapp",
                    detail=f"{new_name}" + (f" ({new_role})" if new_role else ""),
                )
                db.commit()
            except Exception:
                db.rollback()  # duplicado ou outro erro de banco não derruba o fluxo

        elif new_name and from_number.isdigit() and re.search(r"[A-Za-zÀ-ÿ]", new_name):
            # LID ainda sem telefone resolvido: memoriza o nome em memória e
            # registra o marcador para NÃO repetir o convite.
            _lid_name_map[from_number] = {"name": new_name[:160], "role": new_role[:80]}
            _add_step(
                log,
                "nome memorizado (número do grupo ainda não resolvido)",
                detail=new_name,
            )
            db.commit()

        elif contact is None and log.church_id and from_number.isdigit() and len(from_number) >= 10:
            # Sem nome capturado: registra como "já convidado a se identificar"
            # (linha com nome em branco), para a IA NÃO perguntar o nome toda hora.
            try:
                db.add(Contact(church_id=log.church_id, phone=phone_store, name="", role=""))
                db.commit()
            except Exception:
                db.rollback()

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

        # 1) Encaminha a mensagem para o grupo do departamento, se houver grupo
        #    configurado e a pergunta não tenha vindo do próprio grupo dele.
        if matched and matched.get("group_jid") and matched["group_jid"] != log.to_jid:
            sender_label = f"{contact.name} ({from_number})" if contact else from_number
            group_text = (
                f"NOVA MENSAGEM PARA {matched['name'].upper()}\n"
                f"De: {sender_label}\n\n{text}"
            )
            try:
                await evolution.send_text(matched["group_jid"], group_text, instance=instance_name)
                _add_step(log, "encaminhado para o grupo", detail=matched["group_jid"])
            except evolution.EvolutionError as exc:
                logger.error("Falha ao encaminhar para o grupo %s: %s", matched["group_jid"], exc)
                log.error = log.error or ""
                log.error += f" | grupo: {exc}"
                log.status = "routed_with_error"
                _add_step(log, "falha ao encaminhar ao grupo", status="error", detail=str(exc))

        # 2) Envia a resposta da LLM: em grupo, responde no próprio grupo;
        #    no privado, no mesmo JID da conversa (preservando @lid/@s.whatsapp.net)
        if reply:
            reply_jid = log.to_jid or f"{from_number}@s.whatsapp.net"
            try:
                await evolution.send_text(reply_jid, reply, instance=instance_name)
                _add_step(log, "resposta enviada ao grupo" if reply_in_group else "resposta enviada ao membro")
            except evolution.EvolutionError as exc:
                logger.error("Falha ao responder %s: %s", from_number, exc)
                log.error = log.error or ""
                log.error += f" | resposta: {exc}"
                log.status = "routed_with_error"
                _add_step(log, "falha ao enviar resposta", status="error", detail=str(exc))

        db.commit()
        logger.info("Mensagem %s processada (%s)", log_id, department_name)
    except Exception:
        logger.exception("Erro inesperado na tarefa em segundo plano da mensagem %s", log_id)
    finally:
        db.close()


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

    # Roteia para a igreja dona da instância que recebeu a mensagem
    instance_name = (
        payload.get("instance")
        or data.get("instanceName")
        or payload.get("data", {}).get("instance")
        or settings.evolution_instance
    )
    number_row = db.query(WhatsAppNumber).filter(WhatsAppNumber.instance_name == instance_name).first()
    if number_row:
        church_id = number_row.church_id
    else:
        fallback = db.query(Church).order_by(Church.id).first()
        church_id = fallback.id if fallback else None

    key = data.get("key") or {}
    remote_jid = key.get("remoteJid") or data.get("remoteJid") or ""
    from_me = bool(key.get("fromMe") or data.get("fromMe"))

    is_group = remote_jid.endswith("@g.us")
    _remember_lid(key)  # aprende @lid -> telefone sempre que a Evolution informar os dois

    # Ignora mensagens enviadas por nós e canais (aceita JIDs @s.whatsapp.net e @lid)
    if from_me or remote_jid.endswith("@broadcast"):
        return {"ok": True, "skipped": "not_private_or_from_me"}

    # Em grupos, só responde quando o grupo está vinculado a um departamento ativo
    # da igreja dona da instância (configurado no painel em Departamentos).
    if is_group:
        linked_department = (
            db.query(Department)
            .filter(
                Department.active == True,  # noqa: E712
                Department.church_id == church_id,
                Department.group_jid == remote_jid,
            )
            .first()
        )
        if not linked_department:
            return {"ok": True, "skipped": "group_not_linked"}

    message_id = key.get("id") or ""
    if _dedup(message_id):
        return {"ok": True, "skipped": "duplicate"}

    text = _extract_text(data)
    media_key = None
    if _is_audio(data):
        text = ""
        msg = data.get("message") or {}
        audio = msg.get("audioMessage") or msg.get("ptvMessage") or msg.get("voiceMessage") or {}
        media_key = audio.get("mediaKey") or message_id

    if not text and not media_key:
        return {"ok": True, "skipped": "no_text"}

    # Em grupos, guarda quem perguntou (participante); a resposta volta para o grupo.
    # Usa o número real quando a Evolution o informa (participantPn/senderPn), pois
    # os novos JIDs @lid do WhatsApp não são telefones.
    from_number = _sender_phone(data, key, is_group) or _normalize_jid(remote_jid)

    # Participante @lid sem número resolvido: consulta os participantes do grupo
    # para aprender a correspondência e reconhecer o contato em qualquer grupo.
    learn_info = ""
    if is_group and (key.get("participant") or "").endswith("@lid"):
        lid_key = _normalize_jid(key["participant"])
        mapped = _lid_map.get(lid_key)
        if not mapped:
            total, learned = await _learn_group_lids(instance_name, remote_jid)
            learn_info = f"{learned} de {total} participantes com telefone informado"
        mapped = mapped or _lid_map.get(lid_key)
        if mapped:
            from_number = mapped

    log = MessageLog(
        direction="in",
        church_id=church_id,
        from_number=from_number,
        to_jid=remote_jid,
        text=text,
        status="received",
        media_key=media_key or None,
        media_message_id=message_id or None,
    )
    db.add(log)
    db.commit()
    db.refresh(log)

    _add_step(log, "mensagem recebida", detail=f"de: {from_number}")
    if is_group:
        _add_step(log, "pergunta em grupo", detail=linked_department.name)
        participant_jid = key.get("participant") or ""
        if participant_jid.endswith("@lid"):
            if from_number != _normalize_jid(participant_jid):
                _add_step(
                    log,
                    "participante resolvido",
                    detail=f"{participant_jid} -> {from_number}",
                )
            else:
                _add_step(
                    log,
                    "participante @lid sem telefone no grupo",
                    status="warn",
                    detail=learn_info or "busca ainda não realizada",
                )
    db.commit()
    logger.info("Mensagem %s recebida de %s: %s", log.id, from_number, text[:80] or "(áudio)")

    # Processa em segundo plano para responder 200 imediatamente
    # (a Evolution espera resposta em até 60s; áudio/transcrição podem demorar mais).
    task = asyncio.create_task(_process_message(log.id, instance_name))
    task.add_done_callback(lambda t: logger.error("Background task failed: %s", t.exception()) if t.exception() else None)

    return {"ok": True, "processing": True}
