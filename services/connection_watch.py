"""Vigia a conexão do WhatsApp da IA e avisa o administrador.

Loop assíncrono iniciado no lifespan do FastAPI:
- a cada N segundos consulta o estado real de cada instância (mesma checagem do painel);
- ao DESCONECTAR: tenta avisar na hora (falha em silêncio se a sessão cair de vez);
- ao VOLTAR: envia o relatório com o horário e a duração da queda, garantindo que
  o administrador fique sabendo mesmo se a sessão ficou totalmente offline.

NÃO cria segunda sessão nem cliente: usa a mesma instância Evolution existente.
"""

import asyncio
import logging
from datetime import datetime

from database import SessionLocal
from models import AuthorizedUser, Church, WhatsAppNumber
from services import evolution
from services.evolution import EvolutionError
from services.safety import safe_send

logger = logging.getLogger(__name__)

_TICK_SECONDS = 45

# Estado observado por instância (mantido em memória: não precisa de migração).
_prev_state: dict[str, str] = {}
_down_since: dict[str, datetime] = {}


def _destino(phone: str) -> str:
    d = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(d) in (10, 11):
        return "55" + d
    return d


def _admin_numbers(church_id: int) -> list[str]:
    """Números dos perfis 'administrador' ativos, para notificar a queda."""
    with SessionLocal() as db:
        rows = (
            db.query(AuthorizedUser)
            .filter(
                AuthorizedUser.church_id == church_id,
                AuthorizedUser.status == "active",
                AuthorizedUser.profile == "administrador",
            )
            .all()
        )
        return [r.phone for r in rows if (r.phone or "").strip()]


async def _notify(church_id: int, instance: str, text: str) -> None:
    for phone in _admin_numbers(church_id):
        try:
            await safe_send(
                church_id, _destino(phone), text,
                instance=instance, message_id=f"wconn:{instance}:{phone}:{int(asyncio.get_event_loop().time())}",
            )
            logger.info("Queda do WhatsApp notificada ao admin %s (instance %s)", phone, instance)
        except Exception as exc:  # noqa: BLE001 — sessão offline esperada; não derruba o loop
            logger.warning("Falha ao notificar queda ao admin %s: %s", phone, exc)


async def _check(instance: str, church_id: int) -> None:
    try:
        state = await evolution.ping(instance, max_retries=1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Falha ao checar estado da instância %s: %s", instance, exc)
        state = "unknown"

    last = _prev_state.get(instance)
    _prev_state[instance] = state
    connected = state == "open"

    if connected:
        since = _down_since.pop(instance, None)
        if since is not None:
            mins = int((datetime.utcnow() - since).total_seconds() // 60)
            await _notify(
                church_id, instance,
                "✅ O WhatsApp da assistente voltou a conectar.\n"
                f"Ficou desconectada por {mins} min desde {since.strftime('%H:%M')}."
                + ("\n\nSe acabou de conectar agora, a IA já está respondendo novamente." if mins == 0 else ""),
            )
        return

    if last == "open" and state != "open" and _down_since.get(instance) is None:
        # Acabou de desconectar: tenta avisar na hora (pode falhar se a sessão cair de vez).
        _down_since[instance] = datetime.utcnow()
        await _notify(
            church_id, instance,
            "⚠️ O WhatsApp da assistente acabou de DESCONECTAR.\n"
            "Ela não está respondendo no momento. Assim que reconectar, você recebe o aviso.",
        )


async def run_loop() -> None:
    logger.info("Vigia de conexão do WhatsApp iniciado")
    while True:
        try:
            with SessionLocal() as db:
                rows = (
                    db.query(WhatsAppNumber)
                    .filter(WhatsAppNumber.active == True)  # noqa: E712
                    .all()
                )
                instances = [(r.instance_name, r.church_id) for r in rows]
            for instance, church_id in instances:
                try:
                    await _check(instance, church_id)
                except Exception:
                    logger.exception("Falha ao vigiar instância %s", instance)
        except Exception:
            logger.exception("Ciclo do vigia de conexão falhou")
        await asyncio.sleep(_TICK_SECONDS)