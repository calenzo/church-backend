"""Automação de lembretes de aniversário (100% backend, sem depender do painel).

Loop assíncrono iniciado no lifespan do FastAPI:
- compara a hora atual (America/Sao_Paulo) com o horário configurado na aba;
- se já passou do horário e o lembrete de HOJE ainda não foi confirmado, processa;
- agrupa todos os aniversariantes do dia em UMA mensagem por destinatário;
- envia pela sessão WhatsApp já existente (Evolution), SEM criar outra instância;
- registra histórico individual por destinatário com dedupe ano-mês-dia;
- falha temporária -> status 'pendente' e nova tentativa no mesmo dia
  (reinício do backend NUNCA duplica o envio).
"""

import asyncio
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from database import SessionLocal
from models import BirthdayConfig, BirthdayLog, BirthdayRecipient, Church, Member, WhatsAppNumber
from services import evolution
from services.evolution import EvolutionError, send_text

logger = logging.getLogger(__name__)

_TZ = ZoneInfo("America/Sao_Paulo")
_TICK_SECONDS = 30


def send_target_digits(phone: str) -> str:
    """Garante DDI 55 no destino (mesma regra do webhook)."""
    d = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(d) in (10, 11):
        return "55" + d
    return d


async def resolve_instance(church_id: int) -> str | None:
    """Instância ativa e realmente conectada da igreja; None se nenhuma estiver aberta."""
    with SessionLocal() as db:
        numbers = (
            db.query(WhatsAppNumber)
            .filter(WhatsAppNumber.church_id == church_id, WhatsAppNumber.active == True)  # noqa: E712
            .order_by(WhatsAppNumber.id)
            .all()
        )
        instances = [n.instance_name for n in numbers]
    for instance in instances:
        try:
            if await evolution.ping(instance, max_retries=1) == "open":
                return instance
        except Exception:
            continue
    return None


def build_message(names: list[str], ref_date: date, church_name: str) -> str:
    """Uma mensagem única mesmo com vários aniversariantes."""
    dd_mm = f"{ref_date.day:02d}/{ref_date.month:02d}"
    if len(names) == 1:
        return (
            "🎂 Aniversariante de hoje\n\n"
            f"Hoje é aniversário de {names[0]} — {dd_mm}.\n\n"
            f"Lembrete automático da {church_name}."
        )
    bullets = "\n".join(f"• {n}" for n in names)
    return (
        f"🎂 Aniversariantes de hoje — {dd_mm}\n\n"
        "Hoje temos:\n"
        f"{bullets}\n\n"
        f"Lembrete automático da {church_name}."
    )


async def _deliver(db, church: Church, recipients, names: list[str], ref_date: date) -> None:
    message = build_message(names, ref_date, church.name)
    members_text = ", ".join(names)
    instance = await resolve_instance(church.id)

    for recipient in recipients:
        log = (
            db.query(BirthdayLog)
            .filter(
                BirthdayLog.church_id == church.id,
                BirthdayLog.ref_date == ref_date,
                BirthdayLog.recipient_id == recipient.id,
                BirthdayLog.kind == "aniversario",
            )
            .first()
        )
        if log and log.status == "enviado":
            continue  # já confirmado hoje: nunca duplica
        if not log:
            log = BirthdayLog(
                church_id=church.id,
                recipient_id=recipient.id,
                recipient_name=recipient.name or "",
                phone=recipient.phone or "",
                kind="aniversario",
                ref_date=ref_date,
            )
            db.add(log)
            db.flush()

        log.members_text = members_text
        log.message = message
        if not instance:
            log.status = "pendente"
            log.error = "WhatsApp desconectado no momento do envio"
            db.commit()
            continue
        try:
            await send_text(send_target_digits(recipient.phone), message, instance=instance)
            log.status = "enviado"
            log.error = ""
            log.sent_at = datetime.utcnow()
        except EvolutionError as exc:
            # Não perde o aviso: fica pendente e o próximo ciclo tenta de novo.
            log.status = "pendente"
            log.error = str(exc)[:500]
        db.commit()


async def process_day_for_church(church_id: int, now_sp: datetime) -> bool:
    """Processa o lembrete do dia de uma igreja. Retorna True se havia aniversariantes."""
    ref_date = now_sp.date()
    with SessionLocal() as db:
        church = db.get(Church, church_id)
        config = db.query(BirthdayConfig).filter(BirthdayConfig.church_id == church_id).first()
        members = (
            db.query(Member)
            .filter(Member.church_id == church_id, Member.birth_day == ref_date.day, Member.birth_month == ref_date.month)
            .order_by(Member.name)
            .all()
        )
        if not church or not config or not members:
            return False
        recipients = (
            db.query(BirthdayRecipient)
            .filter(BirthdayRecipient.church_id == church_id, BirthdayRecipient.active == True)  # noqa: E712
            .order_by(BirthdayRecipient.id)
            .all()
        )
        if not recipients:
            return False
        await _deliver(db, church, recipients, [m.name for m in members], ref_date)
        return True


async def tick() -> None:
    now_sp = datetime.now(_TZ)
    with SessionLocal() as db:
        rows = db.query(BirthdayConfig.church_id, BirthdayConfig.send_time).all()
    for church_id, send_time in rows:
        try:
            hh, mm = (send_time or "08:00").split(":")[:2]
            target = now_sp.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        except ValueError:
            continue
        # Já passou (ou é) o horário -> garante o envio do dia; o histórico
        # confirma o que já foi enviado, então ciclos/reinícios não duplicam.
        if now_sp >= target:
            try:
                had = await process_day_for_church(church_id, now_sp)
                if had:
                    logger.info("Lembrete de aniversários verificado para igreja %s", church_id)
            except Exception:
                logger.exception("Falha ao processar lembretes da igreja %s", church_id)


async def run_loop() -> None:
    logger.info("Agendador de aniversários iniciado (fuso %s)", _TZ)
    while True:
        try:
            await tick()
        except Exception:
            logger.exception("Ciclo do agendador de aniversários falhou")
        await asyncio.sleep(_TICK_SECONDS)
