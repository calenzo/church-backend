import asyncio
import json
import logging
import re
import time
import unicodedata
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from config import settings
from database import SessionLocal, get_db
from models import AdminAction, AuthorizedUser, Church, Contact, ContactMemory, Department, LLMConfig, MessageLog, RoutingRule, WhatsAppNumber
from services import evolution, llm
from services.admin_commands import analyze_admin_command
from services.phone import canonical as canonical_phone, only_digits, variants as phone_variants
from services.safety import safe_send, get_church_safety

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
    return only_digits(phone)


# Mapeamento aprendido em tempo real: JID @lid -> número real (só dígitos).
# O WhatsApp novo identifica participantes de grupo por LID, que não é telefone.
_lid_map: dict[str, str] = {}
# Nome informado pelo próprio remetente quando o número ainda não foi resolvido
# (LID sem pareamento): vale enquanto o processo estiver de pé.
_lid_name_map: dict[str, dict[str, str]] = {}

# Proteção contra duplicidade: (church_id, numero, regra) -> último envio.
# Várias mensagens seguidas sobre o mesmo assunto geram UM encaminhamento só.
_forward_recently: dict[tuple, datetime] = {}
_FORWARD_WINDOW = 600  # segundos


def _fmt_phone(digits: str) -> str:
    """Exibe o telefone no formato brasileiro quando possível."""
    d = _digits(digits)
    if len(d) in (12, 13) and d.startswith("55"):
        d = d[2:]  # remove o DDI 55 para exibir
    if len(d) == 11:
        return f"({d[:2]}) {d[2:7]}-{d[7:]}"
    if len(d) == 10:
        return f"({d[:2]}) {d[2:6]}-{d[6:]}"
    return digits


def _send_target(phone: str) -> str:
    """Garante DDI 55 no destino do encaminhamento."""
    d = _digits(phone)
    if d and len(d) in (10, 11):
        return "55" + d
    return d


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


def _load_memory_block(db: Session, contact: Contact | None) -> str:
    """Monta o bloco 'Memória do contato': ficha, pendências abertas e fatos recentes.
    Memórias temporárias expiradas são ignoradas."""
    if contact is None or not getattr(contact, "id", None):
        return ""
    now = datetime.utcnow()
    parts = []
    ficha = []
    if (contact.contact_type or "").strip():
        ficha.append(f"tipo: {contact.contact_type}")
    if (contact.department_name or "").strip():
        ficha.append(f"departamento: {contact.department_name}")
    if (contact.last_intent or "").strip():
        ficha.append(f"última intenção: {contact.last_intent}")
    if ficha:
        parts.append("Ficha: " + "; ".join(ficha) + ".")
    if (contact.resumo_contexto or "").strip():
        parts.append(f"Resumo do contexto: {contact.resumo_contexto}")

    actives = (
        db.query(ContactMemory)
        .filter(
            ContactMemory.contact_id == contact.id,
            (ContactMemory.expires_at.is_(None))
            | (ContactMemory.memory_type == "permanente")
            | (ContactMemory.expires_at >= now),
        )
        .order_by(ContactMemory.created_at.desc(), ContactMemory.id.desc())
        .limit(20)
        .all()
    )
    pends = [m for m in actives if m.kind == "pendencia" and m.status != "resolvida"]
    facts = [m for m in actives if m.kind in ("fato", "observacao")][:6]
    if pends:
        lines = [
            f"- {m.content}" + (f" (responsável: {m.responsible})" if m.responsible else "")
            for m in pends[:5]
        ]
        parts.append("Pendências abertas:\n" + "\n".join(lines))
    if facts:
        lines = [f"- {m.content}" for m in facts]
        parts.append("Fatos/observações recentes:\n" + "\n".join(lines))
    if not parts:
        return ""
    return "\n\nMemória do contato:\n" + "\n".join(parts)


def _build_directory(db: Session, church_id: int | None, limit: int = 150) -> str:
    """Monta o bloco 'Diretório de contatos' com nome, cargo e telefone da base.
    Permite à IA informar o contato de membros/líderes cadastrados sem inventar."""
    if not church_id:
        return ""
    rows = (
        db.query(Contact)
        .filter(Contact.church_id == church_id, Contact.name != "")
        .order_by(Contact.name)
        .limit(limit)
        .all()
    )
    if not rows:
        return ""
    lines = []
    for c in rows:
        label = c.name.strip() + (f" ({c.role.strip()})" if (c.role or "").strip() else "")
        lines.append(f"- {label}: {_fmt_phone(c.phone)}")
    return (
        "\n\nDiretório de contatos da igreja (agenda oficial; pode informar "
        "telefone e cargo destas pessoas quando pedirem):\n" + "\n".join(lines)
    )


def _strip_accents(value: str) -> str:
    return "".join(
        ch
        for ch in unicodedata.normalize("NFD", value.lower())
        if unicodedata.category(ch) != "Mn"
    )


# Palavras de pergunta/estrutura que NUNCA identificam um contato na agenda.
_DIRECTORY_STOPWORDS = {
    "qual", "quais", "quem", "numero", "número", "telefone", "contato", "contatos",
    "whatsapp", "zap", "sabe", "diga", "passa", "manda", "pode", "favor", "como",
    "falo", "falar", "preciso", "queria", "gostaria", "informacao", "informação",
    "cadastro", "cadastrado", "cadastrada", "igreja", "pessoa", "alguem", "alguém",
    "onde", "encontro", "acha", "achar", "meu", "minha", "esse", "essa", "para",
    "por", "com", "sem", "uma", "tem", "aqui", "entao", "então",
}


def _directory_hits(db: Session, church_id: int | None, text: str, limit: int = 3) -> str:
    """Busca determinística na agenda: pessoas citadas na mensagem por NOME ou CARGO.
    Reforça o prompt da LLM com os prováveis alvos da pergunta (modelos pequenos
    costumam ignorar o diretório quando perguntam por cargo, ex.: 'a secretária')."""
    if not church_id or not text:
        return ""
    tokens = [
        _strip_accents(t)
        for t in re.split(r"[^A-Za-zÀ-ÿ]+", text)
        if len(t) >= 4
    ]
    tokens = [t for t in tokens if t not in _DIRECTORY_STOPWORDS]
    if not tokens:
        return ""
    rows = (
        db.query(Contact)
        .filter(Contact.church_id == church_id, Contact.name != "")
        .all()
    )
    scored = []
    for c in rows:
        name = _strip_accents(c.name or "")
        role = _strip_accents(c.role or "")
        score = sum(1 for t in tokens if t in name or (role and t in role))
        if score:
            scored.append((score, c))
    if not scored:
        return ""
    scored.sort(key=lambda item: item[0], reverse=True)
    lines = []
    for _, c in scored[:limit]:
        label = c.name.strip() + (f" ({c.role.strip()})" if (c.role or "").strip() else "")
        lines.append(f"- {label}: {_fmt_phone(c.phone)}")
    return (
        "\n\nBusca na agenda para ESTA mensagem — se algum item corresponde ao que "
        "foi pedido, INFORME o telefone cadastrado na sua resposta:\n"
        + "\n".join(lines)
    )


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
    Aceita qualquer formatação: '219999069940', '55219999069940', '+55 21 99906-9940'
    e afins apontam para o MESMO cadastro (normalização central em services/phone).
    Prefere contatos com nome; linhas em branco (marcadores) ficam por último.
    Para LIDs ainda não pareados, usa o nome que o próprio remetente informou."""
    digits = _digits(phone)
    if not digits or not church_id:
        return None
    candidates = phone_variants(digits)
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
# Apresentação sem artigo: "sou Jennifer", "aqui é Jennifer" (nome começa com maiúscula).
_SELF_NAME_BARE_RE = re.compile(
    r"(?:^|[^\wÀ-ÿ])(?:sou|aqui (?:é|e))\s+"
    r"([A-ZÀ-Þ][\wÀ-ÿ']*(?:\s+[A-Za-zÀ-ÿ][\wÀ-ÿ']*){0,3})"
)
_NAME_PARTICLES = {"de", "da", "do", "das", "dos", "e"}
_NAME_NOT_NAMES = {"eu", "mim", "nos", "nós", "ele", "ela", "nada", "ninguem", "ninguém"}
_ROLE_WORDS = {
    "irmã", "irma", "irmão", "irmao", "missionária", "missionaria", "diaconisa",
    "diácono", "diacono", "presbítero", "presbitero", "evangelista",
    "pastor", "pastora", "reverendo",
}


def _clean_name_words(words: list[str]) -> str:
    """Mantém palavras que parecem nome (iniciais maiúsculas/partículas) e
    descarta o resto ('da limpeza', 'aqui' etc.), sem inventar dados."""
    kept: list[str] = []
    for word in words:
        low = word.lower()
        if not kept or low in _NAME_PARTICLES or word[:1].isupper():
            kept.append(word)
        else:
            break
    while kept and kept[-1].lower() in _NAME_PARTICLES:
        kept.pop()
    return " ".join(kept)


def _extract_self_name(text: str) -> tuple[str, str]:
    """Extrai (nome, cargo) de frases de apresentação, sem depender da LLM."""
    match = _SELF_NAME_RE.search(text or "")
    role = ""
    if match:
        raw = match.group(1)
    else:
        match = _SELF_NAME_BARE_RE.search(text or "")
        if not match:
            return "", ""
        raw = _clean_name_words(match.group(1).strip().strip("!,.?;:").split())
    words = raw.strip().strip("!,.?;:").split()
    name_words = []
    for word in words:
        clean = word.strip(".,")
        if not name_words and clean.lower() in _ROLE_WORDS:
            role = clean.title()
        else:
            name_words.append(clean)
    name = " ".join(name_words)
    if (
        not (2 <= len(name) <= 60)
        or not re.search(r"[A-Za-zÀ-ÿ]", name)
        or name.lower() in _NAME_NOT_NAMES
    ):
        return "", ""
    return name, role


def apply_self_registration(
    db: Session, church_id: int | None, phone: str, name: str, role: str
) -> str:
    """Cadastra (ou completa) o contato a partir do que o próprio remetente disse.
    O CADASTRO OFICIAL tem prioridade: só preenche campos VAZIOS — nunca altera
    nome/função já registrados. Telefone é normalizado antes de salvar/consultar,
    então '+55 21 97388-6107' e '21973886107' não geram duplicidade.
    Retorna 'criado', 'atualizado' ou '' (nada mudou / falha de banco)."""
    if not (church_id and name and re.search(r"[A-Za-zÀ-ÿ]", name)):
        return ""
    row = _lookup_contact(db, church_id, phone)
    try:
        if row:
            changed = False
            if not (row.name or "").strip():
                row.name = name[:160]
                changed = True
            if role and not (row.role or "").strip():
                row.role = role[:80]
                changed = True
            if changed:
                db.commit()
                return "atualizado"
            return ""
        store = canonical_phone(phone) or only_digits(phone)
        if not store:
            return ""
        db.add(Contact(church_id=church_id, phone=store[:20], name=name[:160], role=role[:80]))
        db.commit()
        return "criado"
    except Exception:
        db.rollback()  # duplicado ou outro erro de banco não derruba o fluxo
        return ""


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

        # ── Secretária Inteligente: comandos administrativos via WhatsApp ──
        if text and from_number and log.church_id:
            try:
                admin = analyze_admin_command(db, log.church_id, from_number, text)
            except Exception:
                logger.exception("Erro ao analisar comando admin de %s", from_number)
                admin = None
            if admin and admin.recognized:
                _add_step(log, "comando admin reconhecido", detail=admin.intent)
                reply_jid = log.to_jid or f"{from_number}@s.whatsapp.net"
                final_reply = admin.reply
                send_result = "none"

                # Ação real: enviar aviso ao grupo WhatsApp vinculado ao departamento.
                # Só informa sucesso após o retorno real da API/WhatsApp.
                targets_group = (
                    admin.intent == "enviar_aviso_grupo"
                    and bool(admin.action_data.get("message"))
                    and bool(admin.action_data.get("group_id"))
                    and not admin.action_data.get("draft_only")
                )
                if targets_group:
                    group_id = admin.action_data["group_id"]
                    group_name = (
                        admin.action_data.get("group_name")
                        or admin.action_data.get("department")
                        or "grupo"
                    )
                    message = admin.action_data["message"]
                    dept_name = admin.action_data.get("department", "")
                    try:
                        result = await safe_send(
                            log.church_id or 1,
                            group_id,
                            message,
                            instance=instance_name,
                            message_id=f"admin-group:{log.id}",
                            is_reply=False,
                        )
                        if result.get("ok"):
                            send_result = "success"
                            final_reply = f"Aviso enviado ao grupo {group_name}."
                        else:
                            send_result = result.get("status", "error")
                            final_reply = f"Não consegui enviar o aviso ao grupo {group_name}."
                            detail = result.get("detail", "")
                            if detail:
                                log.error = (log.error or "") + f" | enviar_aviso_grupo: {detail}"
                    except evolution.EvolutionError as exc:
                        send_result = "error"
                        final_reply = f"Não consegui enviar o aviso ao grupo {group_name}."
                        log.error = (log.error or "") + f" | enviar_aviso_grupo: {exc}"
                        log.status = "routed_with_error"
                        logger.error("Falha ao enviar aviso ao grupo %s: %s", group_id, exc)
                    logger.info(
                        "[COMMAND] enviar_aviso_grupo [AUTHORIZED_USER] true "
                        "[DEPARTMENT] %s [GROUP_ID] %s [MESSAGE] %s [SEND_RESULT] %s",
                        dept_name or "?", group_id, message, send_result,
                    )
                    _add_step(log, f"envio ao grupo: {send_result}", detail=group_id[:40])

                try:
                    await safe_send(
                        log.church_id or 1, reply_jid, final_reply,
                        instance=instance_name, message_id=f"admin-reply:{log.id}",
                    )
                    _add_step(log, "resposta admin enviada", detail=admin.intent)
                except evolution.EvolutionError as exc:
                    logger.error("Falha ao responder admin %s: %s", from_number, exc)
                    log.status = "routed_with_error"
                    _add_step(log, "falha ao enviar resposta admin", status="error", detail=str(exc))
                # Registra a ação no audit log
                try:
                    db.add(AdminAction(
                        church_id=log.church_id,
                        user_name=admin.action_data.get("source_user", ""),
                        phone=from_number,
                        raw_command=text[:500],
                        intent=admin.intent,
                        action=f"status={send_result}",
                        target=admin.action_data.get("group_id", ""),
                        department=admin.action_data.get("department", ""),
                        new_value=admin.action_data.get("message", ""),
                        status="executado" if targets_group else "recebido",
                    ))
                except Exception:
                    db.rollback()
                db.commit()
                log.department_name = "admin_whatsapp"
                log.llm_reply = final_reply
                log.status = "routed"
                db.commit()
                return

        # Regras de encaminhamento ativas: assunto -> responsável. A IA decide
        # automaticamente quando usar (sem opções manuais no painel).
        rules_rows = (
            db.query(RoutingRule)
            .filter(RoutingRule.church_id == log.church_id, RoutingRule.active == True)  # noqa: E712
            .all()
        )
        routing_rules = [
            {"id": r.id, "topic": r.topic, "responsible": r.responsible} for r in rules_rows
        ]
        rules_map = {str(r.id): r for r in rules_rows}

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
            memory_text = _load_memory_block(db, contact)
            # Diretório da agenda oficial (igreja inteira): disponível em conversas
            # privadas E em todos os grupos, para a IA informar nome/cargo/telefone
            # de quem está cadastrado sem recusar nem inventar.
            directory_text = _build_directory(db, log.church_id)
            hits = _directory_hits(db, log.church_id, text)
            if hits:
                directory_text += hits
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
                routing_rules=routing_rules,
                memory_text=memory_text,
                directory_text=directory_text,
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

        # Encaminhamento automático: a IA apontou uma regra; o sistema executa
        # e só confirma ao remetente se o envio realmente aconteceu.
        rule = rules_map.get(str(result.get("forward_rule_id") or "")) if config.apply_routing_rules else None
        # Escrita automática de memória: exige contato persistido e não bloqueado.
        persistable = bool(
            contact is not None
            and getattr(contact, "id", None)
            and log.church_id
            and not contact.memory_locked
        )
        if rule:
            alvo = rule.responsible.strip() or "o responsável pelo assunto"
            if known and contact:
                remetente = contact.name + (f" ({contact.role})" if contact.role else "")
            else:
                remetente = "número não identificado"
            dedup_key = (log.church_id, from_number, rule.id)
            last_sent = _forward_recently.get(dedup_key)
            if (
                last_sent
                and (datetime.utcnow() - last_sent).total_seconds() < _FORWARD_WINDOW
            ):
                # Mesma solicitação em sequência: não notifica o responsável de novo.
                reply = f'Sua solicitação sobre "{rule.topic}" já foi encaminhada para {alvo}.'
                _add_step(log, "encaminhamento repetido ignorado", status="warn", detail=alvo)
                db.commit()
            else:
                destino = _send_target(rule.phone)
                notificacao = (
                    f"Nova solicitação — {rule.topic}\n"
                    f"Remetente: {remetente}\n"
                    f"Telefone: {_fmt_phone(from_number)}\n"
                    f'Mensagem: "{text[:500]}"'
                )
                try:
                    await safe_send(
                        log.church_id or 1, destino, notificacao,
                        instance=instance_name, message_id=f"fw:{dedup_key}",
                    )
                    _forward_recently[dedup_key] = datetime.utcnow()
                    reply = f"Sua mensagem foi encaminhada para {alvo}."
                    _add_step(log, "encaminhado para o responsável", detail=f"{alvo} ({destino})")
                    if persistable:
                        # Registra a pendência: alguém precisa responder essa pessoa.
                        db.add(
                            ContactMemory(
                                church_id=log.church_id,
                                contact_id=contact.id,
                                kind="pendencia",
                                content=f"{rule.topic} - aguardando {alvo}",
                                responsible=alvo[:120],
                                status="aberta",
                                source="automatica",
                            )
                        )
                        contact.last_intent = f"encaminhar: {rule.topic}"[:160]
                        contact.last_talk_at = datetime.utcnow()
                    db.commit()
                except evolution.EvolutionError as exc:
                    logger.error("Falha ao encaminhar para %s (%s): %s", alvo, destino, exc)
                    reply = (
                        f"Não consegui encaminhar sua mensagem para {alvo} agora. "
                        "Tente novamente mais tarde."
                    )
                    log.status = "routed_with_error"
                    _add_step(
                        log, "falha ao encaminhar ao responsável", status="error", detail=str(exc)
                    )
                    db.commit()

        # Cadastro automático: nome dito pelo remetente (LLM) ou extraído da frase.
        if config.auto_register_contacts:
            new_name = (result.get("new_contact_name") or "").strip()
            new_role = (result.get("new_contact_role") or "").strip()
            if not new_name and not known:
                fb_name, fb_role = _extract_self_name(text)
                if fb_name:
                    new_name, new_role = fb_name, (new_role or fb_role)
            phone_ok = bool(
                log.church_id and from_number.isdigit() and 10 <= len(from_number) <= 13
            )  # telefones reais; LIDs têm ~15+ dígitos

            if new_name and phone_ok:
                outcome = apply_self_registration(db, log.church_id, from_number, new_name, new_role)
                if outcome:
                    # Telefone é normalizado dentro do helper: sem duplicidade de formato.
                    _add_step(
                        log,
                        "contato salvo pelo whatsapp",
                        detail=f"{new_name}" + (f" ({new_role})" if new_role else ""),
                    )
                    db.commit()

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
                    db.add(
                        Contact(
                            church_id=log.church_id,
                            phone=canonical_phone(from_number)[:20],
                            name="",
                            role="",
                        )
                    )
                    db.commit()
                except Exception:
                    db.rollback()

        # ---- memória automática: guarda só o que for útil para o futuro ----
        if config.auto_memory:
            intent = result.get("intent", "")
            note = result.get("memory_note", "")
            pendency = result.get("new_pendency", "")
            ctype = result.get("contact_type", "")
            if persistable:
                updates = []
                if intent and intent != contact.last_intent:
                    contact.last_intent = intent[:160]
                    updates.append(f"intenção: {intent}")
                if (
                    ctype
                    and ctype.lower() not in ("desconhecido", "não identificado")
                    and ctype != contact.contact_type
                ):
                    # Só chega aqui quando a pessoa DECLAROU explicitamente o que é.
                    contact.contact_type = ctype[:40]
                    updates.append(f"tipo: {ctype}")
                if note:
                    db.add(
                        ContactMemory(
                            church_id=log.church_id,
                            contact_id=contact.id,
                            kind="fato",
                            content=note[:500],
                            source="automatica",
                        )
                    )
                    updates.append(f"fato: {note}")
                if pendency:
                    db.add(
                        ContactMemory(
                            church_id=log.church_id,
                            contact_id=contact.id,
                            kind="pendencia",
                            content=pendency[:500],
                            status="aberta",
                            source="automatica",
                        )
                    )
                    updates.append(f"pendente: {pendency}")
                contact.last_talk_at = datetime.utcnow()
                if updates:
                    _add_step(log, "memória do contato atualizada", detail="; ".join(updates)[:200])
                db.commit()

        matched = next((d for d in departments if d["name"].lower() == department_name.lower()), None)
        matched_dep = None
        if matched:
            matched_dep = db.query(Department).filter(Department.name == matched["name"]).first()

        log.department_name = department_name
        log.department_id = matched_dep.id if matched_dep else None
        log.llm_reply = reply
        # Preserva o status de erro caso o encaminhamento ao responsável tenha falhado.
        log.status = log.status if log.status == "routed_with_error" else "routed"
        _add_step(log, "classificado", detail=f"departamento: {department_name}")
        db.commit()

        # 1) Encaminha a mensagem para o grupo do departamento, se houver grupo
        #    configurado e a pergunta não tenha vindo do próprio grupo dele.
        if config.forward_to_groups and matched and matched.get("group_jid") and matched["group_jid"] != log.to_jid:
            sender_label = f"{contact.name} ({from_number})" if contact else from_number
            group_text = (
                f"NOVA MENSAGEM PARA {matched['name'].upper()}\n"
                f"De: {sender_label}\n\n{text}"
            )
            try:
                await safe_send(
                    log.church_id or 1, matched["group_jid"], group_text,
                    instance=instance_name, message_id=f"grp:{log.id}",
                )
                _add_step(log, "encaminhado para o grupo", detail=matched["group_jid"])
            except evolution.EvolutionError as exc:
                logger.error("Falha ao encaminhar para o grupo %s: %s", matched["group_jid"], exc)
                log.error = log.error or ""
                log.error += f" | grupo: {exc}"
                log.status = "routed_with_error"
                _add_step(log, "falha ao encaminhar ao grupo", status="error", detail=str(exc))

        # 2) Envia a resposta da LLM: em grupo, responde no próprio grupo;
        #    no privado, no mesmo JID da conversa (preservando @lid/@s.whatsapp.net)
        if reply and config.auto_reply:
            reply_jid = log.to_jid or f"{from_number}@s.whatsapp.net"
            try:
                result = await safe_send(
                    log.church_id or 1, reply_jid, reply,
                    instance=instance_name, message_id=f"reply:{log.id}",
                )
                if result.get("status") in ("paused", "blocked"):
                    _add_step(log, f"resposta bloqueada: {result.get('status')}", status="warn")
                else:
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

    # Verifica permissões do webhook antes de processar
    wconfig = get_or_create_config(db, church_id)
    if is_group and not wconfig.process_groups:
        return {"ok": True, "skipped": "groups_disabled"}
    if not is_group and not wconfig.process_private:
        return {"ok": True, "skipped": "private_disabled"}

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
    is_audio = _is_audio(data)
    if is_audio:
        if not wconfig.process_audio:
            return {"ok": True, "skipped": "audio_disabled"}
        text = ""
        msg = data.get("message") or {}
        audio = msg.get("audioMessage") or msg.get("ptvMessage") or msg.get("voiceMessage") or {}
        media_key = audio.get("mediaKey") or message_id

    if text and not wconfig.process_text:
        return {"ok": True, "skipped": "text_disabled"}

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
