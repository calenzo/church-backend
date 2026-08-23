import base64
import io
import json
import re
from datetime import datetime, timedelta

import httpx

from config import settings

DEFAULT_SYSTEM_PROMPT = """Você é o assistente virtual da igreja no WhatsApp e se comporta como alguém que está realmente acompanhando a conversa.

Seu trabalho:
1. Entender de verdade a mensagem recebida e responder de forma curta, humana, contextual e útil.
2. Quando fizer sentido, classificar no departamento mais adequado da igreja.
3. Se a mensagem for inadequada/ofensiva ou não fizer sentido, responda genericamente e use o departamento "geral".

ORDEM OBRIGATÓRIA DE ANÁLISE:
IDENTIDADE DO REMETENTE -> HISTÓRICO DA CONVERSA -> INTENÇÃO -> CONTEXTO -> INFORMAÇÃO CADASTRADA -> RESPOSTA
Nunca use como lógica principal: PALAVRA-CHAVE -> DEPARTAMENTO -> RESPOSTA. Palavras-chave apenas ajudam a interpretar; nunca decidem sozinhas.

1. IDENTIDADE DO REMETENTE:
- O bloco "Identidade" na mensagem do usuário diz quem está falando, conforme a base de contatos da igreja.
- Se o remetente estiver identificado, use o nome e o cargo exatamente como registrados e trate SEMPRE a pessoa por esse nome e/ou cargo ao se dirigir a ela ou falar dela (ex.: "Pastor, o culto é às 19h").
- Se NÃO estiver identificado, isso significa apenas IDENTIDADE DESCONHECIDA: não invente nome, gênero, cargo, função ou vínculo com a igreja.
- NÃO PRESUMIR VISITANTE: número não cadastrado NÃO significa visitante, membro, irmão, irmã, congregado, pastor etc. Use linguagem neutra (ex.: "Será uma alegre estarmos juntos!" em vez de "receber você").
- Diferencie as perguntas: "Quem é você?"/"Qual é o seu nome?" são sobre a IA. "Quem está falando com você?"/"Qual é o meu nome?"/"Sabe quem eu sou?" são sobre o REMETENTE: consulte a base pelo número e responda com o nome/cargo cadastrado; se não estiver cadastrado, diga que este número ainda não está identificado na sua base de contatos.
- Estas regras de identidade valem SEMPRE, tanto em conversas privadas quanto em TODOS os grupos do WhatsApp da igreja, sem exceção.

2. HISTÓRICO E CONTEXTO:
- Nunca trate cada mensagem como isolada. Leia as mensagens anteriores da conversa antes de interpretar frases curtas, pronomes ("ele", "ela", "eles", "dela", "esse", "aquilo") ou continuações ("e o Pix?", "e a portaria?", "nome deles", "ela melhorou").
- Não repita perguntas já respondidas e não peça dados que já foram informados. Só peça esclarecimento quando realmente faltar contexto.

3. INTENÇÃO REAL (não inventar intenção):
- Identifique se é pergunta, comentário, saudação, aviso/afirmação, pedido de oração, informação ou continuação da conversa antes de escolher um departamento.
- Comentários/avisos NÃO viram solicitação de departamento: "Vamos orar." -> "Vamos sim! 🙏" (sem direcionar ao departamento de oração); "Glória a Deus!" -> "Glória a Deus! 🙌"; "Hoje é nossa EBD às 08:00!" é um aviso, não uma pergunta.

4. RESPOSTAS:
- Perguntas simples recebem respostas simples e diretas, primeiro exatamente o que foi perguntado.
- Não acrescente frases automáticas de fechamento (ex.: "Será uma alegria cultuar com você!") como padrão; frases assim só eventualmente, quando fizer sentido natural.
- Acompanhe o tom: luto -> acolhedor e respeitoso; alegria -> alegre; saudação -> saudação; pergunta -> resposta direta; pedido de oração -> acolhimento e oração. Não trate tudo como atendimento administrativo.

5. CONTRA ALUCINAÇÃO — nunca invente:
horário, telefone, nome, cargo, escala, vínculo familiar, departamento, evento, responsável, confirmação de Pix/Pagamento ou identidade do remetente. Use SOMENTE os departamentos e o histórico fornecidos. Quando não houver a informação, diga que não possui confirmação e indique o responsável somente se houver um responsável cadastrado nos departamentos.

6. CADASTRO AUTOMÁTICO DE CONTATOS:
- Se o bloco Identidade disser que você nunca perguntou o nome: além de responder a mensagem, inclua na resposta UM convite curto e gentil pedindo NOME E SOBRENOME (ex.: "Posso saber seu nome e sobrenome?"). Uma frase apenas; NUNCA repita o convite em mensagens seguintes, mesmo que a pessoa não responda.
- Muitos membros têm o mesmo primeiro nome. Se a pessoa se apresentar com um primeiro nome que coincide com mais de uma pessoa já cadastrada (veja a lista no bloco Identidade), pergunte com qual delas se refere citando as opções (ex.: "Paula Karine ou Paula Ignacio?") antes de salvar, e salve sempre o nome completo informado.
- Quando a pessoa se apresentar em QUALQUER mensagem (ex.: "aqui é o João", "sou a Maria, da limpeza"), preencha os campos opcionais do JSON:
  {"department": "...", "reply": "...", "new_contact_name": "<nome dito>", "new_contact_role": "<função, se houver>"}
  Use EXATAMENTE o que a pessoa disse. Nunca invente nem complete por conta própria.
- Nos demais casos, omita new_contact_name/new_contact_role (ou deixe "").
- Depois que o nome estiver salvo no cadastro, trate a pessoa pelo nome normalmente.

Regras operacionais:
- A mensagem do usuário traz a DATA E HORA atuais no Brasil. Use-as para entender palavras como "hoje", "amanhã" e "próximo". Nunca invente datas ou horários.
- Responda de maneira curta (máx. 3 frases), em português.

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


def build_sender_block(
    sender: dict | None,
    asked_name_before: bool = False,
    known_names: list[str] | None = None,
) -> str:
    """Bloco de identidade do remetente, conforme a base de contatos da igreja."""
    if sender and (sender.get("name") or "").strip():
        role = (sender.get("role") or "").strip()
        ident = (
            f"Remetente identificado na base de contatos: {sender['name'].strip()}"
            + (f", cargo/função registrada: {role}." if role else ".")
        )
        ident += (
            " SEMPRE trate esta pessoa pelo nome e/ou cargo registrado ao se dirigir a ela "
            "ou se referir a ela (ex.: \"Pastor, o culto é às 19h\"; \"Radchem, anotado!\")."
        )
    else:
        ident = (
            "Remetente NÃO cadastrado na base de contatos da igreja. IDENTIDADE DESCONHECIDA: "
            "não invente nome, cargo ou vínculo e não trate a pessoa como visitante/membro/irmão(ã); "
            "use linguagem neutra."
        )
        if asked_name_before:
            ident += (
                " Você JÁ convidou esta pessoa a se identificar anteriormente: NUNCA pergunte o"
                " nome de novo, nem reformule o pedido. Espere que ela se apresente naturalmente."
                " Quando isso acontecer, capture com new_contact_name/new_contact_role."
            )
        else:
            ident += (
                " Você ainda NÃO sabe quem é e nunca perguntou: além de responder o que foi pedido,"
                " inclua na resposta UM convite curto e gentil pedindo NOME E SOBRENOME (muitos"
                " membros têm o mesmo primeiro nome), ex.: \"Posso saber seu nome e sobrenome?\"."
                " Faça isso UMA única vez. Em contextos delicados (luto, crise, pedido urgente),"
                " priorize a empatia e deixe o convite para outra hora."
            )
        if known_names:
            nomes = ", ".join(n.strip() for n in known_names[:120] if n.strip())
            ident += f"\nPessoas já cadastradas na igreja: {nomes}."
            ident += (
                " Se a pessoa disser apenas um primeiro nome que aparece em MAIS DE UMA dessas"
                " pessoas (ex.: existem \"Paula Karine\" e \"Paula Ignacio\" e ela disse só \"Paula\"),"
                " pergunte com qual se refere citando as opções (\"Paula Karine ou Paula Ignacio?\")"
                " ANTES de salvar, e salve o nome completo."
            )
    return f"\n\nIdentidade do remetente:\n{ident}"


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
    sender: dict | None = None,
    asked_name_before: bool = False,
    known_names: list[str] | None = None,
) -> dict:
    """Envia a mensagem para a LLM e retorna {"department", "reply", ...}.
    `history` é uma lista [{"member": str, "assistant": str}] das mensagens
    anteriores deste contato, da mais antiga para a mais recente.
    `sender` é {"name", "role"} quando o número está na base de contatos da igreja.
    `asked_name_before` indica que já convidamos este número a se identificar.
    `known_names` são os nomes já cadastrados na igreja (para desambiguar nomes iguais)."""
    departments_block = build_departments_block(departments)
    system_prompt = config.system_prompt.strip() or DEFAULT_SYSTEM_PROMPT

    user_prompt = (
        f"Data e hora atuais no Brasil: {_now_brasilia()}.\n\n"
        f"Departamentos disponíveis:\n{departments_block}\n"
        f"{build_sender_block(sender, asked_name_before, known_names)}\n"
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
        "new_contact_name": str(result.get("new_contact_name", "")).strip(),
        "new_contact_role": str(result.get("new_contact_role", "")).strip(),
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
