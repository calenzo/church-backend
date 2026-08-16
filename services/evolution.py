import httpx

from config import settings


class EvolutionError(Exception):
    pass


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if settings.evolution_api_key:
        headers["apikey"] = settings.evolution_api_key
    return headers


async def send_text(number: str, text: str) -> dict:
    """Envia mensagem de texto via Evolution API para um número ou grupo (JID)."""
    url = f"{settings.evolution_base_url.rstrip('/')}/message/sendText/{settings.evolution_instance}"
    payload = {"number": number, "text": text}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=_headers())
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        raise EvolutionError(f"Falha ao enviar mensagem pela Evolution API: {exc}") from exc


async def list_groups() -> list[dict]:
    """Retorna a lista de grupos do WhatsApp da instância: [{"id", "subject"}]."""
    url = f"{settings.evolution_base_url.rstrip('/')}/group/fetchAllGroups/{settings.evolution_instance}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, params={"getParticipants": "false"}, headers=_headers())
            resp.raise_for_status()
            data = resp.json()
        return [{"id": g.get("id"), "subject": g.get("subject") or g.get("id")} for g in data]
    except httpx.HTTPError as exc:
        raise EvolutionError(f"Falha ao listar grupos da Evolution API: {exc}") from exc


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


async def get_media_base64(message_id: str, convert_to_mp4: bool = True) -> str | None:
    """Baixa a mídia de uma mensagem recebida e retorna o base64."""
    url = f"{settings.evolution_base_url.rstrip('/')}/chat/getBase64FromMediaMessage/{settings.evolution_instance}"
    payload = {"message": {"key": {"id": message_id}}, "convertToMp4": convert_to_mp4}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=_headers())
            resp.raise_for_status()
            data = resp.json()
        if isinstance(data, str):
            return data
        return data.get("base64") or data.get("media") or data.get("file")
    except httpx.HTTPError as exc:
        raise EvolutionError(f"Falha ao baixar mídia da Evolution API: {exc}") from exc


async def ping() -> str:
    """Verifica se a instância da Evolution API está conectada."""
    url = f"{settings.evolution_base_url.rstrip('/')}/instance/connectionState/{settings.evolution_instance}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=_headers())
            resp.raise_for_status()
            state = resp.json().get("instance", {}).get("state", "unknown")
            return state
    except httpx.HTTPError as exc:
        raise EvolutionError(f"Evolution API indisponível: {exc}") from exc
