import asyncio
import logging
import time

import httpx

from config import settings

logger = logging.getLogger(__name__)

# Cache em memória para grupos, por instância (evita bater na Evolution API toda página)
_groups_cache: dict[str, dict] = {}
GROUPS_CACHE_TTL = 60  # 1 minuto


class EvolutionError(Exception):
    pass


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if settings.evolution_api_key:
        headers["apikey"] = settings.evolution_api_key
    return headers


async def send_text(number: str, text: str, instance: str | None = None, max_retries: int = 3) -> dict:
    """Envia mensagem de texto via Evolution API para um número ou grupo (JID).
    Inclui retry com backoff para lidar com cold-start da Evolution API."""
    inst = instance or settings.evolution_instance
    url = f"{settings.evolution_base_url.rstrip('/')}/message/sendText/{inst}"
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


async def list_groups(
    instance: str | None = None,
    max_retries: int = 3,
    force_refresh: bool = False,
) -> list[dict]:
    """Retorna a lista de grupos do WhatsApp da instância: [{"id", "subject"}].
    Usa cache em memória com TTL de 5 minutos. force_refresh=True ignora o cache."""
    inst = instance or settings.evolution_instance
    cache = _groups_cache.setdefault(inst, {"data": None, "ts": 0.0})
    now = time.monotonic()
    if not force_refresh and cache["data"] is not None and (now - cache["ts"]) < GROUPS_CACHE_TTL:
        return cache["data"]

    base = settings.evolution_base_url.rstrip("/")
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

        cache["data"] = result
        cache["ts"] = time.monotonic()
        return result

    raise EvolutionError(
        f"Falha ao listar grupos após {max_retries} tentativas (rate-limit do WhatsApp): {last_error}"
    )


async def get_qrcode(instance: str | None = None) -> str | None:
    """Retorna o QR code (base64) para parear o WhatsApp, ou None se já conectado.
    Se a instância não existir mais (404), tenta recriá-la antes de buscar o QR."""
    inst = instance or settings.evolution_instance
    try:
        state = await ping(inst)
    except EvolutionError:
        # Instância ausente ou API instável: segue para conectar/recriar.
        state = None
    if state == "open":
        return None
    data = await _connect_with_autocreate(instance=inst)
    qrobj = data.get("qrcode") or data
    b64 = qrobj.get("base64") if isinstance(qrobj, dict) else None
    if not b64:
        # Instância recém-criada ou reconectando: o QR pode levar alguns segundos.
        raise EvolutionError(
            "O WhatsApp ainda esta gerando o QR code desta conexao; aguarde alguns segundos."
        )
    return b64


async def create_instance(instance: str) -> None:
    """Cria a instância na Evolution API (novo número ou recriação após reset)."""
    url = f"{settings.evolution_base_url.rstrip('/')}/instance/create"
    payload = {
        "instanceName": instance,
        "qrcode": True,
        "integration": "WHATSAPP-BAILEYS",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=_headers())
    except httpx.HTTPError as exc:
        raise EvolutionError(f"Falha ao comunicar com a Evolution API: {exc}") from exc
    if resp.status_code not in (200, 201):
        logger.warning("Falha ao criar instância (HTTP %d): %s", resp.status_code, resp.text[:300])
        raise EvolutionError(
            f"A Evolution API recusou a criacao da instancia (HTTP {resp.status_code})."
        )
    logger.info("Instância '%s' criada na Evolution API", instance)


async def _connect_raw(number: str | None = None, instance: str | None = None) -> dict:
    inst = instance or settings.evolution_instance
    url = f"{settings.evolution_base_url.rstrip('/')}/instance/connect/{inst}"
    params = {"number": number} if number else None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params, headers=_headers())
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        raise EvolutionError(f"Falha ao conectar instância na Evolution API: {exc}") from exc


async def _connect_with_autocreate(number: str | None = None, instance: str | None = None) -> dict:
    """Conecta a instância; se ela não existir mais (sessão resetada), cria e tenta de novo."""
    try:
        return await _connect_raw(number, instance)
    except EvolutionError as exc:
        logger.warning("Connect falhou (%s); criando instância antes de tentar novamente", exc)
        await create_instance(instance or settings.evolution_instance)
        await asyncio.sleep(2)
        return await _connect_raw(number, instance)


async def logout_instance(instance: str | None = None) -> dict:
    """Faz logout da instância para desconectar o WhatsApp e limpar credenciais salvas.
    Necessário para que o pairing code funcione (a instância não pode estar 'registered')."""
    inst = instance or settings.evolution_instance
    try:
        state = await ping(inst)
    except EvolutionError:
        state = None

    if state is not None and state != "open":
        return {"disconnected": False, "already_disconnected": True}

    url = f"{settings.evolution_base_url.rstrip('/')}/instance/logout/{inst}"
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
            state = await ping(inst)
        except EvolutionError:
            state = None
        if state != "open":
            logger.warning("Logout retornou erro (%s) mas a instância não está mais conectada", exc)
            invalidate_groups_cache(inst)
            return {"disconnected": True}
        # Sessão corrompida (ex.: arquivos perdidos em disco efêmero): apaga a
        # instância como último recurso para permitir reconexão limpa.
        logger.warning("Logout falhou (%s) e instância segue '%s'; apagando instância para resetar", exc, state)
        await delete_instance(inst)
        return {"disconnected": True, "instance_deleted": True}
    invalidate_groups_cache(inst)
    logger.info("Logout da instância realizado com sucesso")
    return {"disconnected": True}


async def delete_instance(instance: str | None = None) -> None:
    """Apaga a instância na Evolution API (registro e credenciais salvas).
    Usado para limpar sessões corrompidas que nem o logout aceita."""
    inst = instance or settings.evolution_instance
    url = f"{settings.evolution_base_url.rstrip('/')}/instance/delete/{inst}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.delete(url, headers=_headers())
            if resp.status_code == 404:
                return
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise EvolutionError(f"Falha ao apagar instância na Evolution API: {exc}") from exc
    logger.info("Instância apagada na Evolution API")
    invalidate_groups_cache(inst)


def invalidate_groups_cache(instance: str | None = None) -> None:
    if instance:
        _groups_cache.pop(instance, None)
    else:
        _groups_cache.clear()


async def get_pairing_code(number: str, instance: str | None = None) -> dict:
    """Retorna dados de conexão: pairingCode e/ou QR code (base64).
    O número deve estar no formato 5511999999999 (código do país + DDD + número).
    Faz logout da instância antes para limpar credenciais e permitir pairing code."""
    inst = instance or settings.evolution_instance
    state = await ping(inst)
    logger.info("get_pairing_code: connection state = %s", state)
    if state == "open":
        return {"connected": True}

    try:
        await logout_instance(inst)
    except EvolutionError as exc:
        logger.warning("Falha ao limpar credenciais antes do pairing code: %s", exc)
    await asyncio.sleep(3)

    data = await _connect_with_autocreate(number, inst)
    pairing = data.get("pairingCode")
    base64_qr = data.get("base64")
    if pairing:
        return {"pairingCode": pairing}
    if base64_qr:
        return {"pairingCode": None, "qrcode": base64_qr}
    raise EvolutionError("Resposta da API não contém pairingCode nem qrcode")


async def get_media_base64(message_id: str, remote_jid: str = "", from_me: bool = False, convert_to_mp4: bool = False, instance: str | None = None, max_retries: int = 3) -> str | None:
    """Baixa a mídia de uma mensagem recebida e retorna o base64 (sem prefixo data URI).
    Inclui retry com backoff para lidar com cold-start da Evolution API."""
    inst = instance or settings.evolution_instance
    url = f"{settings.evolution_base_url.rstrip('/')}/chat/getBase64FromMediaMessage/{inst}"
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


async def ping(instance: str | None = None, max_retries: int = 2) -> str:
    """Verifica se a instância da Evolution API está conectada."""
    inst = instance or settings.evolution_instance
    url = f"{settings.evolution_base_url.rstrip('/')}/instance/connectionState/{inst}"
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
