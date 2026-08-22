import asyncio
import logging
import time

import httpx

from config import settings

logger = logging.getLogger(__name__)

# Cache em memória para grupos (evita bater na Evolution API toda página)
_groups_cache: dict = {"data": None, "ts": 0.0}
GROUPS_CACHE_TTL = 300  # 5 minutos


class EvolutionError(Exception):
    pass


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if settings.evolution_api_key:
        headers["apikey"] = settings.evolution_api_key
    return headers


async def send_text(number: str, text: str, max_retries: int = 3) -> dict:
    """Envia mensagem de texto via Evolution API para um número ou grupo (JID).
    Inclui retry com backoff para lidar com cold-start da Evolution API."""
    url = f"{settings.evolution_base_url.rstrip('/')}/message/sendText/{settings.evolution_instance}"
    payload = {"number": number, "text": text}
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=payload, headers=_headers())
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt < max_retries - 1:
                logger.warning("send_text attempt %d/%d failed: %s", attempt + 1, max_retries, exc)
                await asyncio.sleep(2 ** attempt * 2)
    raise EvolutionError(f"Falha ao enviar mensagem após {max_retries} tentativas: {last_error}")


async def list_groups(max_retries: int = 3, force_refresh: bool = False) -> list[dict]:
    """Retorna a lista de grupos do WhatsApp da instância: [{"id", "subject"}].
    Usa cache em memória com TTL de 5 minutos. force_refresh=True ignora o cache."""
    now = time.monotonic()
    if not force_refresh and _groups_cache["data"] is not None and (now - _groups_cache["ts"]) < GROUPS_CACHE_TTL:
        return _groups_cache["data"]

    base = settings.evolution_base_url.rstrip("/")
    inst = settings.evolution_instance
    url = f"{base}/group/fetchAllGroups/{inst}"

    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(url, params={"getParticipants": "false"}, headers=_headers())
        except Exception as exc:
            last_error = exc
            logger.warning("list_groups attempt %d/%d connect error: %s", attempt + 1, max_retries, exc)
            await asyncio.sleep(2 ** attempt)
            continue

        if resp.status_code == 429 or resp.status_code >= 500:
            body = resp.text[:300]
            last_error = EvolutionError(f"HTTP {resp.status_code}: {body}")
            logger.warning(
                "list_groups attempt %d/%d got HTTP %d, retrying...",
                attempt + 1, max_retries, resp.status_code,
            )
            await asyncio.sleep(2 ** attempt * 3)
            continue

        if resp.status_code != 200:
            raise EvolutionError(
                f"Evolution API retornou HTTP {resp.status_code} em GET {url}: {resp.text[:300]}"
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise EvolutionError(
                f"Resposta não-JSON da Evolution API ({resp.status_code}): {resp.text[:300]}"
            ) from exc

        groups_raw: list[dict] = []
        if isinstance(data, list):
            groups_raw = data
        elif isinstance(data, dict):
            groups_raw = data.get("groups") or data.get("instances") or []
        else:
            raise EvolutionError(f"Formato inesperado: {type(data).__name__}")

        result = []
        for g in groups_raw:
            if not isinstance(g, dict):
                continue
            result.append({"id": g.get("id"), "subject": g.get("subject") or g.get("id")})

        _groups_cache["data"] = result
        _groups_cache["ts"] = time.monotonic()
        return result

    raise EvolutionError(
        f"Falha ao listar grupos após {max_retries} tentativas (rate-limit do WhatsApp): {last_error}"
    )


async def get_qrcode() -> str | None:
    """Retorna o QR code (base64) para parear o WhatsApp, ou None se já conectado."""
    try:
        state = await ping()
    except EvolutionError:
        raise
    if state == "open":
        return None
    url = f"{settings.evolution_base_url.rstrip('/')}/instance/connect/{settings.evolution_instance}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=_headers())
            resp.raise_for_status()
            data = resp.json()
            qr = data.get("qrcode") or data
            return qr.get("base64")
    except httpx.HTTPError as exc:
        raise EvolutionError(f"Falha ao obter QR code da Evolution API: {exc}") from exc


async def _restart_instance() -> None:
    """Reinicia a instância da Evolution API (necessário para gerar pairing code)."""
    url = f"{settings.evolution_base_url.rstrip('/')}/instance/restart/{settings.evolution_instance}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.put(url, headers=_headers())
            resp.raise_for_status()
            logger.info("Instância reiniciada com sucesso")
    except httpx.HTTPError as exc:
        logger.warning("Falha ao reiniciar instância: %s", exc)


async def logout_instance() -> dict:
    """Faz logout da instância para desconectar o WhatsApp e limpar credenciais salvas.
    Necessário para que o pairing code funcione (a instância não pode estar 'registered')."""
    try:
        state = await ping()
    except EvolutionError:
        state = None

    if state is not None and state != "open":
        return {"disconnected": False, "already_disconnected": True}

    url = f"{settings.evolution_base_url.rstrip('/')}/instance/logout/{settings.evolution_instance}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.delete(url, headers=_headers())
            if resp.status_code == 404:
                return {"disconnected": False, "already_disconnected": True}
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        # Algumas versões da Evolution retornam 404/500 ao deslogar instância
        # já desconectada ou durante cold-start. Confere o estado real antes de falhar.
        try:
            state = await ping()
        except EvolutionError:
            state = None
        if state != "open":
            logger.warning("Logout retornou erro (%s) mas a instância não está mais conectada", exc)
            _invalidate_groups_cache()
            return {"disconnected": True}
        raise EvolutionError(f"Falha ao desconectar a instância na Evolution API: {exc}") from exc
    _invalidate_groups_cache()
    logger.info("Logout da instância realizado com sucesso")
    return {"disconnected": True}


def _invalidate_groups_cache() -> None:
    _groups_cache["data"] = None
    _groups_cache["ts"] = 0.0


async def get_pairing_code(number: str, max_retries: int = 3) -> dict:
    """Retorna dados de conexão: pairingCode e/ou QR code (base64).
    O número deve estar no formato 5511999999999 (código do país + DDD + número).
    Faz logout da instância antes para limpar credenciais e permitir pairing code."""
    try:
        state = await ping()
    except EvolutionError:
        raise
    logger.info("get_pairing_code: connection state = %s", state)
    if state == "open":
        return {"connected": True}

    try:
        await logout_instance()
    except EvolutionError as exc:
        logger.warning("Falha ao limpar credenciais antes do pairing code: %s", exc)
    await asyncio.sleep(3)

    url = f"{settings.evolution_base_url.rstrip('/')}/instance/connect/{settings.evolution_instance}"
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, params={"number": number}, headers=_headers())
                resp.raise_for_status()
                data = resp.json()
                logger.info("Evolution connect response for pairing (tentativa %d): %s", attempt + 1, data)
                pairing = data.get("pairingCode")
                base64_qr = data.get("base64")
                if pairing:
                    return {"pairingCode": pairing}
                if base64_qr:
                    return {"pairingCode": None, "qrcode": base64_qr}
                last_error = EvolutionError("Resposta da API não contém pairingCode nem qrcode")
        except httpx.HTTPError as exc:
            last_error = exc
        if attempt < max_retries - 1:
            await asyncio.sleep(3)
    raise EvolutionError(f"Falha ao obter código de pareamento após {max_retries} tentativas: {last_error}")


async def get_media_base64(message_id: str, remote_jid: str = "", from_me: bool = False, convert_to_mp4: bool = False, max_retries: int = 3) -> str | None:
    """Baixa a mídia de uma mensagem recebida e retorna o base64 (sem prefixo data URI).
    Inclui retry com backoff para lidar com cold-start da Evolution API."""
    url = f"{settings.evolution_base_url.rstrip('/')}/chat/getBase64FromMediaMessage/{settings.evolution_instance}"
    payload = {
        "message": {
            "key": {
                "id": message_id,
                "remoteJid": remote_jid,
                "fromMe": from_me,
            }
        },
        "convertToMp4": convert_to_mp4,
    }
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=payload, headers=_headers())
                resp.raise_for_status()
                data = resp.json()
            b64 = None
            if isinstance(data, str):
                b64 = data
            else:
                b64 = data.get("base64") or data.get("media") or data.get("file")
            if b64 and "," in b64 and b64.startswith("data:"):
                b64 = b64.split(",", 1)[1]
            return b64
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt < max_retries - 1:
                logger.warning("get_media_base64 attempt %d/%d failed: %s", attempt + 1, max_retries, exc)
                await asyncio.sleep(2 ** attempt * 2)
    raise EvolutionError(f"Falha ao baixar mídia após {max_retries} tentativas: {last_error}")


async def ping(max_retries: int = 2) -> str:
    """Verifica se a instância da Evolution API está conectada."""
    url = f"{settings.evolution_base_url.rstrip('/')}/instance/connectionState/{settings.evolution_instance}"
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, headers=_headers())
                resp.raise_for_status()
                state = resp.json().get("instance", {}).get("state", "unknown")
                return state
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt < max_retries - 1:
                await asyncio.sleep(2)
    raise EvolutionError(f"Evolution API indisponível após {max_retries} tentativas: {last_error}")
