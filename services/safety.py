"""Fila de envio anti-spam / anti-bloqueio para WhatsApp.

Camada obrigatória entre o sistema e a Evolution API.
Controla: fila com atraso variável, rate limiting, detecção de duplicatas,
cooldown por contato, debounce de mensagens, modos de proteção e emergência.
"""

import asyncio
import logging
import random
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from services.evolution import EvolutionError, send_text

logger = logging.getLogger(__name__)

_TZ = ZoneInfo("America/Sao_Paulo")


# ── Estados de proteção ──────────────────────────────────────────────

class ProtectionMode(str, Enum):
    NORMAL = "NORMAL"
    ATENCAO = "ATENCAO"
    PROTECAO = "PROTECAO"
    PAUSADO = "PAUSADO"


# ── Configuração por igreja ──────────────────────────────────────────

@dataclass
class RateLimits:
    min_delay_sec: float = 5.0        # atraso mínimo entre envios
    max_delay_sec: float = 10.0       # atraso máximo entre envios
    max_per_minute: int = 10
    max_per_hour: int = 50
    cooldown_sec: float = 3.0         # cooldown por contato
    debounce_sec: float = 5.0         # debounce para agrupamento


# ── Métricas ─────────────────────────────────────────────────────────

@dataclass
class Metrics:
    sent_minute: int = 0
    sent_hour: int = 0
    received_minute: int = 0
    received_hour: int = 0
    duplicate_blocked: int = 0
    protection_blocked: int = 0
    total_sent: int = 0
    total_received: int = 0
    last_minute_reset: float = 0.0
    last_hour_reset: float = 0.0
    last_error: str = ""
    last_disconnect: str = ""
    last_reconnect: str = ""


# ── Item na fila ─────────────────────────────────────────────────────

@dataclass
class QueueItem:
    message_id: str
    church_id: int
    number: str
    text: str
    instance: str
    priority: bool = False          #True = responder (não inicia conversa)
    created_at: float = field(default_factory=time.monotonic)
    attempts: int = 0


# ── Gerenciador de segurança por igreja ──────────────────────────────

class ChurchSafety:
    def __init__(self, church_id: int):
        self.church_id = church_id
        self.mode = ProtectionMode.NORMAL
        self.limits = RateLimits()
        self.metrics = Metrics()
        self.queue: asyncio.Queue[QueueItem] = asyncio.Queue()
        self._processed_ids: set[str] = set()
        self._id_order: deque[str] = deque(maxlen=500)
        self._contact_last_sent: dict[str, float] = {}
        self._recent_texts: dict[str, float] = {}
        self._worker_task: asyncio.Task | None = None
        self._paused = False

    def _reset_counters_if_needed(self):
        now = time.monotonic()
        if now - self.metrics.last_minute_reset >= 60:
            self.metrics.sent_minute = 0
            self.metrics.received_minute = 0
            self.metrics.last_minute_reset = now
        if now - self.metrics.last_hour_reset >= 3600:
            self.metrics.sent_hour = 0
            self.metrics.received_hour = 0
            self.metrics.last_hour_reset = now

    def is_duplicate(self, message_id: str) -> bool:
        if not message_id:
            return False
        if message_id in self._processed_ids:
            self.metrics.duplicate_blocked += 1
            return True
        self._processed_ids.add(message_id)
        self._id_order.append(message_id)
        if len(self._id_order) >= 500:
            old = self._id_order[0]
            self._processed_ids.discard(old)
        return False

    def check_rate_limit(self) -> bool:
        self._reset_counters_if_needed()
        if self.mode == ProtectionMode.PAUSADO:
            self.metrics.protection_blocked += 1
            return False
        if self.mode == ProtectionMode.PROTECAO:
            if self.metrics.sent_minute >= 2:
                self.metrics.protection_blocked += 1
                return False
        if self.metrics.sent_minute >= self.limits.max_per_minute:
            self.metrics.protection_blocked += 1
            return False
        if self.metrics.sent_hour >= self.limits.max_per_hour:
            self.metrics.protection_blocked += 1
            return False
        return True

    def check_cooldown(self, number: str) -> bool:
        now = time.monotonic()
        last = self._contact_last_sent.get(number, 0)
        return (now - last) >= self.limits.cooldown_sec

    def check_debounce(self, number: str, text: str) -> str | None:
        now = time.monotonic()
        key = f"{number}:{text.strip().lower()}"
        last = self._recent_texts.get(key, 0)
        if (now - last) < self.limits.debounce_sec:
            return None
        self._recent_texts[key] = now
        if len(self._recent_texts) > 1000:
            cutoff = now - 120
            self._recent_texts = {
                k: v for k, v in self._recent_texts.items() if v >= cutoff
            }
        return key

    def update_mode_auto(self):
        self._reset_counters_if_needed()
        if self.mode == ProtectionMode.PAUSADO:
            return
        sent = self.metrics.sent_minute
        limit = self.limits.max_per_minute
        if sent >= limit * 0.8 or self.metrics.protection_blocked > 3:
            if self.mode != ProtectionMode.PROTECAO:
                logger.warning(
                    "Igreja %s: entrando em modo PROTEÇÃO (enviados=%d/%d)",
                    self.church_id, sent, limit,
                )
            self.mode = ProtectionMode.PROTECAO
        elif sent >= limit * 0.5:
            if self.mode == ProtectionMode.NORMAL:
                logger.info("Igreja %s: entrando em modo ATENÇÃO", self.church_id)
                self.mode = ProtectionMode.ATENCAO
        else:
            if self.mode in (ProtectionMode.ATENCAO, ProtectionMode.PROTECAO):
                self.mode = ProtectionMode.NORMAL

    def delay_for_item(self) -> float:
        base = random.uniform(self.limits.min_delay_sec, self.limits.max_delay_sec)
        if self.mode == ProtectionMode.PROTECAO:
            base *= 3
        elif self.mode == ProtectionMode.ATENCAO:
            base *= 1.5
        return base

    def record_sent(self, number: str):
        now = time.monotonic()
        self._contact_last_sent[number] = now
        self.metrics.sent_minute += 1
        self.metrics.sent_hour += 1
        self.metrics.total_sent += 1
        self.update_mode_auto()

    def record_received(self):
        now = time.monotonic()
        self.metrics.received_minute += 1
        self.metrics.received_hour += 1
        self.metrics.total_received += 1

    def record_error(self, error: str):
        self.metrics.last_error = error[:200]

    def set_paused(self, paused: bool):
        self._paused = paused
        if paused:
            self.mode = ProtectionMode.PAUSADO
        else:
            self.mode = ProtectionMode.NORMAL

    def snapshot(self) -> dict:
        return {
            "church_id": self.church_id,
            "mode": self.mode.value,
            "paused": self._paused,
            "queue_size": self.queue.qsize(),
            "sent_minute": self.metrics.sent_minute,
            "sent_hour": self.metrics.sent_hour,
            "received_minute": self.metrics.received_minute,
            "received_hour": self.metrics.received_hour,
            "total_sent": self.metrics.total_sent,
            "total_received": self.metrics.total_received,
            "duplicate_blocked": self.metrics.duplicate_blocked,
            "protection_blocked": self.metrics.protection_blocked,
            "last_error": self.metrics.last_error,
            "last_disconnect": self.metrics.last_disconnect,
            "last_reconnect": self.metrics.last_reconnect,
            "limits": {
                "min_delay_sec": self.limits.min_delay_sec,
                "max_delay_sec": self.limits.max_delay_sec,
                "max_per_minute": self.limits.max_per_minute,
                "max_per_hour": self.limits.max_per_hour,
                "cooldown_sec": self.limits.cooldown_sec,
                "debounce_sec": self.limits.debounce_sec,
            },
        }

    def update_limits(self, **kwargs):
        for k, v in kwargs.items():
            if hasattr(self.limits, k) and v is not None:
                setattr(self.limits, k, v)


# ── Singleton global ─────────────────────────────────────────────────

_churches: dict[int, ChurchSafety] = {}


def get_church_safety(church_id: int) -> ChurchSafety:
    if church_id not in _churches:
        _churches[church_id] = ChurchSafety(church_id)
    return _churches[church_id]


# ── Função principal de envio seguro ─────────────────────────────────

async def safe_send(
    church_id: int,
    number: str,
    text: str,
    instance: str,
    message_id: str = "",
    is_reply: bool = True,
) -> dict:
    """Envio seguro que passa por todas as verificações de segurança.

    Retorna: {"ok": True, "status": "sent"/"queued"/"blocked"/"paused"/"duplicate"/"cooldown"}
    """
    safety = get_church_safety(church_id)

    # 1. Verificar se está pausado
    if safety.mode == ProtectionMode.PAUSADO:
        return {"ok": False, "status": "paused", "detail": "Automação pausada pelo administrador"}

    # 2. Verificar duplicata
    if message_id and safety.is_duplicate(message_id):
        logger.info("Igreja %s: mensagem duplicada bloqueada (%s)", church_id, message_id)
        return {"ok": False, "status": "duplicate"}

    # 3. Verificar debounce
    if safety.check_debounce(number, text) is None:
        logger.info("Igreja %s: debounce agrupou mensagem para %s", church_id, number)
        return {"ok": False, "status": "debounce"}

    # 4. Verificar cooldown por contato
    if not safety.check_cooldown(number):
        remaining = safety.limits.cooldown_sec - (time.monotonic() - safety._contact_last_sent.get(number, 0))
        return {"ok": False, "status": "cooldown", "detail": f"Aguarde {remaining:.0f}s"}

    # 5. Verificar rate limit
    if not safety.check_rate_limit():
        safety.update_mode_auto()
        return {
            "ok": False,
            "status": "blocked",
            "detail": f"Rate limit atingido (modo {safety.mode.value})",
        }

    # 6. Calcular atraso
    delay = safety.delay_for_item()

    # 7. Enviar (com atraso se não for reply direto)
    if not is_reply and delay > 0:
        await asyncio.sleep(delay)

    # 8. Enviar de verdade
    try:
        resp = await send_text(number, text, instance=instance)
        safety.record_sent(number)
        logger.info(
            "Igreja %s: enviado para %s (resposta=%s, fila=%s)",
            church_id, number, (resp or {}).get("status", "?"), safety.queue.qsize(),
        )
        return {"ok": True, "status": "sent", "response": resp}
    except EvolutionError as exc:
        safety.record_error(str(exc))
        raise


# ── Worker de fila (para envios em background) ───────────────────────

async def queue_worker(church_id: int):
    """Worker que processa a fila de envios de uma igreja."""
    safety = get_church_safety(church_id)
    logger.info("Worker de segurança iniciado para igreja %s", church_id)
    while True:
        try:
            item = await safety.queue.get()
            if safety.mode == ProtectionMode.PAUSADO:
                await safety.queue.task_done()
                continue
            if safety.is_duplicate(item.message_id):
                await safety.queue.task_done()
                continue
            delay = safety.delay_for_item()
            await asyncio.sleep(delay)
            try:
                resp = await send_text(item.number, item.text, instance=item.instance)
                safety.record_sent(item.number)
                item.attempts += 1
            except EvolutionError as exc:
                safety.record_error(str(exc))
                if item.attempts < 2:
                    item.attempts += 1
                    await safety.queue.put(item)
            await safety.queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Worker de segurança falhou para igreja %s", church_id)
            await asyncio.sleep(5)
