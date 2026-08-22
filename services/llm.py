import base64
import io
import json
import re
from datetime import datetime, timedelta

import httpx

from config import settings

DEFAULT_SYSTEM_PROMPT = """Você é o assistente virtual de atendimento da igreja no WhatsApp.

Seu trabalho é:
1. Classificar a mensagem recebida no departamento mais adequado da igreja.
2. Responder ao membro de forma educada, acolhedora e curta (máx. 3 frases).
3. Se a mensagem for inadequada/ofensiva ou não fizer sentido, responda genericamente
   e use o departamento "geral".

Regras importantes:
- A mensagem do usuário traz a DATA E HORA atuais no Brasil. Use-as para entender
  palavras como "hoje", "amanhã" e "próximo". Nunca invente datas ou horários:
  use apenas as informações dos departamentos e da conversa.
- Existe um HISTÓRICO DA CONVERSA com o membro. Use-o para interpretar perguntas
  curtas como "qual dia?" ou "e amanhã?", que se referem ao que foi dito antes.

Responda SEMPRE apenas com JSON válido no formato:
{"department": "<nome do departamento>", "reply": "<sua resposta>"}

Se nenhum departamento corresponder, use exatamente "geral".
"""

_WEEKDAYS_PT = [
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
]


def _now_brasilia() -> str:
    """Data/hora atual no fuso de Brasília, ex.: 'sábado, 22/08/2026 às 18:22'."""
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("America/Sao_Paulo"))
    except Exception:
        now = datetime.utcnow() - timedelta(hours=3)
    return (
        f"{_WEEKDAYS_PT[now.weekday()]}, "
        f"{now.strftime('%d/%m/%Y')} às {now.strftime('%H:%M')}"
    )


class LlmError(Exception):
    pass


def _normalize_base(base_url: str) -> str:
    """Remove sufixo /v1 (aceita URLs com ou sem o segmento /v1)."""
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


def _endpoint(base_url: str) -> str:
    return _normalize_base(base_url) + "/v1/chat/completions"


def _extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise LlmError("LLM não retornou JSON válido")
    return json.loads(match.group(0))


def build_departments_block(departments: list[dict]) -> str:
    """Monta a lista de departamentos com nome e descrição para o prompt."""
    if not departments:
        return ""
    lines = []
    for dep in departments:
        if dep.get("description"):
            lines.append(f"- {dep['name']}: {dep['description']}")
        else:
            lines.append(f"- {dep['name']}")
    return "\n".join(lines)


def build_history_block(history: list[dict] | None) -> str:
    """Monta o bloco de histórico da conversa (mais antiga -> mais recente)."""
    if not history:
        return ""
    lines = ["", "Histórico da conversa com este membro:"]
    for entry in history:
        member = (entry.get("member") or "").strip()
        assistant = (entry.get("assistant") or "").strip()
        if member:
            lines.append(f"Membro: {member}")
        if assistant:
            lines.append(f"Assistente: {assistant}")
    return "\n".join(lines)


async def classify_and_reply(
    message: str,
    departments: list[dict],
    config,
    history: list[dict] | None = None,
) -> dict:
    """Envia a mensagem para a LLM e retorna {"department", "reply"}.
    `history` é uma lista [{"member": str, "assistant": str}] das mensagens
    anteriores deste contato, da mais antiga para a mais recente."""
    departments_block = build_departments_block(departments)
    system_prompt = config.system_prompt.strip() or DEFAULT_SYSTEM_PROMPT

    user_prompt = (
        f"Data e hora atuais no Brasil: {_now_brasilia()}.\n\n"
        f"Departamentos disponíveis:\n{departments_block}\n"
        f"{build_history_block(history)}\n\n"
        f"Mensagem atual do membro:\n\"{message}\""
    )

    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": config.temperature,
        "response_format": {"type": "json_object"},
        "stream": False,
    }

    try:
        async with httpx.AsyncClient(timeout=settings.llm_timeout) as client:
            resp = await client.post(_endpoint(config.base_url), json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise LlmError(f"Falha ao chamar a LLM em {config.base_url}: {exc}") from exc

    try:
        content = data["choices"][0]["message"]["content"]
        result = _extract_json(content)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise LlmError("Resposta da LLM em formato inesperado") from exc

    return {
        "department": str(result.get("department", "geral")).strip() or "geral",
        "reply": str(result.get("reply", "")).strip(),
    }


async def transcribe_audio(audio_b64: str, config, mime_type: str = "audio/ogg") -> str:
    """Transcreve um áudio (base64) via API OpenAI Whisper."""
    base = _normalize_base(config.base_url)
    url = base + "/v1/audio/transcriptions"

    audio_bytes = base64.b64decode(audio_b64)

    headers = {}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    ext = "ogg" if "ogg" in mime_type else ("mp3" if "mp3" in mime_type else "wav")
    files = {"file": (f"audio.{ext}", io.BytesIO(audio_bytes), mime_type)}
    data = {"model": "whisper-1", "language": "pt"}

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, files=files, data=data, headers=headers)
            resp.raise_for_status()
            result = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise LlmError(f"Falha ao transcrever áudio via API: {exc}") from exc

    text = (result.get("text") or "").strip()
    if not text:
        raise LlmError("Transcrição retornou texto vazio")
    return text


async def ping(base_url: str, model: str, api_key: str = "") -> str:
    """Retorna "ok" se a LLM responder, ou lança LlmError (API compatível com OpenAI)."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            resp = await client.get(base_url.rstrip("/") + "/models", headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise LlmError(f"Não foi possível conectar à LLM em {base_url}: {exc}") from exc
    if not (isinstance(data, dict) and data.get("data")):
        raise LlmError(f"A LLM em {base_url} não respondeu no formato esperado (endpoint /models)")
    return "ok"
