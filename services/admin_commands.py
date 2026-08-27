"""Secretária Inteligente — comandos administrativos via WhatsApp.

Detecta usuários autorizados, interpreta comandos em linguagem natural,
valida permissões e retorna ações estruturadas para o backend executar.
A IA NÃO modifica o banco diretamente — gera intents que o backend valida.
"""

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from models import AuthorizedUser, Department

logger = logging.getLogger(__name__)

# ── Permissões disponíveis ───────────────────────────────────────────

ALL_PERMISSIONS = [
    "ensinar_informacoes",
    "corrigir_informacoes",
    "excluir_informacoes",
    "consultar_escalas",
    "criar_escalas",
    "alterar_escalas",
    "publicar_escalas",
    "criar_avisos",
    "enviar_avisos",
    "agendar_avisos",
    "criar_eventos",
    "alterar_eventos",
    "registrar_decisoes",
    "criar_tarefas",
    "consultar_pendencias",
    "alterar_programacao",
    "publicar_grupo",
    "publicar_geral",
    "administrar_usuarios",
    "consultar_auditoria",
]

PROFILE_DEFAULTS = {
    "administrador": ALL_PERMISSIONS,
    "secretaria": [
        "ensinar_informacoes", "corrigir_informacoes",
        "criar_avisos", "enviar_avisos", "agendar_avisos",
        "criar_eventos", "alterar_eventos",
        "criar_escalas", "alterar_escalas", "consultar_escalas",
        "registrar_decisoes", "criar_tarefas", "consultar_pendencias",
        "alterar_programacao",
    ],
    "lider": [
        "ensinar_informacoes", "corrigir_informacoes",
        "criar_avisos", "enviar_avisos",
        "criar_eventos",
        "criar_escalas", "alterar_escalas", "consultar_escalas",
        "registrar_decisoes", "criar_tarefas", "consultar_pendencias",
    ],
    "operador": [
        "consultar_escalas", "criar_escalas", "alterar_escalas",
        "registrar_decisoes", "consultar_pendencias",
    ],
    "comunicador": [
        "criar_avisos", "enviar_avisos", "agendar_avisos",
    ],
    "instrutor": [
        "ensinar_informacoes", "corrigir_informacoes",
    ],
}


def _parse_permissions(raw: str | list | None) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def get_effective_permissions(user: AuthorizedUser) -> list[str]:
    """Retorna permissões efetivas: base do perfil + extras individuais."""
    base = set(PROFILE_DEFAULTS.get(user.profile, []))
    extras = set(_parse_permissions(user.permissions))
    return sorted(base | extras)


def check_permission(user: AuthorizedUser, perm: str) -> bool:
    if user.status != "active":
        return False
    return perm in get_effective_permissions(user)


# ── Resultado da análise de comando ─────────────────────────────────

@dataclass
class AdminResult:
    recognized: bool = False
    intent: str = ""
    needs_confirmation: bool = False
    reply: str = ""
    action_data: dict = field(default_factory=dict)
    permission_needed: str = ""


# ── Padrões de detecção ─────────────────────────────────────────────

_LEARN_PATTERNS = re.compile(
    r"(aprenda|guarde|anote|salve|lembre[\s-]se|a partir de agora|registre que|informe que)",
    re.IGNORECASE,
)

_CORRECT_PATTERNS = re.compile(
    r"(corrija|corrige|atualize|altere|troque|mude|modifique|não é mais|agora é|não será mais)",
    re.IGNORECASE,
)

_NOTIFY_GROUP_PATTERNS = re.compile(
    r"(avise|avis[ae]|manda|mand[ae]|mande|envie|envia|enviar|coloque|coloca|comunique|informe|informa|comunica|publique|publica|posta|poste)\b",
    re.IGNORECASE,
)

# Palavras que indicam referência explícita a um grupo/departamento.
_GROUP_REFERENCE_PATTERNS = re.compile(
    r"(grupo|group|departamento)",
    re.IGNORECASE,
)

# Separa a menção do alvo ("que", "para", ":", ",") do conteúdo da mensagem.
_MESSAGE_SEP_RE = re.compile(r"^\s*(?:que\s*|para\s*|:\s*|,\s*)+", re.IGNORECASE)


def _message_after(rest: str) -> str:
    """Separa o conteúdo da mensagem do trecho logo após a menção do grupo.

    Se o trecho começar com ':', ',' ou '-' o conteúdo é preservado literalmente
    (ex.: "...grupo Contatos da Igreja: QUE A BICICLETA É DA FATIMA" -> o "QUE"
    faz parte da mensagem e NÃO deve ser removido).
    Caso contrário, remove conectores falados ('que'/'para'/'de') que apenas
    ligam "avise o grupo X ... mensagem"."""
    if re.match(r"^\s*[:,\-]", rest or ""):
        return re.sub(r"^\s*[:,\-]+\s*", "", rest or "").strip().rstrip(".!")
    return _MESSAGE_SEP_RE.sub("", rest or "").strip().rstrip(".!")

# Intervalos de busca do nome do departamento: não muito longe do verbo de envio.
_MAX_TARGET_DISTANCE = 60

# Captura genérica de um grupo citado ("o grupo X", "grupo X:", "no grupo de X").
# O nome para no primeiro conector de mensagem (que/para/sobre).
_GROUP_CLAIM_RE = re.compile(
    r"(?:para\s+(?:o|a)\s+)?(?:no\s+)?(?:grupo|group|departamento)"
    r"\s*(?:de|do|da|dos|das)?\s*:?\s*"
    r"([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9\-]*"
    r"(?:\s+(?!(?:que|para|sobre)\b)[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9\-]*){0,7})"
    r"(?=\s*(?::\s*|,\s*|que\b|para\b|sobre\b|$))",
    re.IGNORECASE,
)

# Consulta ao status real do último envio ("Já enviou?", "Foi?", "Mandou?", "Deu certo?").
_SEND_STATUS_QUERY_PATTERNS = re.compile(
    r"(^(foi\?|foi\s+m[ée]smo\?|deu\s+certo\?|deu\s+certo\s+o\s+envio\?|"
    r"mandou\?|enviou\?|enviad[oa]\?)|"
    r"(já|ja)\s+(?:foi\s+)?(enviou|mandou|enviad[oa]|mandado)|"
    r"(foi\s+(enviado|mandado|enviada))|"
    r"(o\s+aviso\s+foi|o\s+envio\s*foi|deu\s+certo+o\s+envio)|"
    r"chegou\s+(no\s+grupo|nos\s+grupos)|e\s+o\s+aviso)",
    re.IGNORECASE,
)

_SCHEDULE_PATTERNS = re.compile(
    r"(agende|agendar|lembrete para|avise.* às|avise.* amanhã|avise.* segunda|avise.* terça|avise.* quarta|avise.* quinta|avise.* sexta|avise.* sábado|avise.* domingo)",
    re.IGNORECASE,
)

_DRAFT_PATTERNS = re.compile(
    r"(prepare|rascunho|escreva a mensagem|não envie|só escreva|não mande)",
    re.IGNORECASE,
)

_EXACT_SEND_PATTERNS = re.compile(
    r"(envie exatamente|mande exatamente|envia exatamente|manda exatamente|envie isto|mande isto)",
    re.IGNORECASE,
)

_IMPROVE_SEND_PATTERNS = re.compile(
    r"(melhore|melhora|escreva melhor|redija|reescreva|faça uma versão melhor)",
    re.IGNORECASE,
)

_SCHEDULE_SCALE_PATTERNS = re.compile(
    r"(crie a escala|criar escala|monte a escala|montar escala|nova escala)",
    re.IGNORECASE,
)

_CHANGE_SCALE_PATTERNS = re.compile(
    r"(troque|substitua|coloque|retire|remova).*(escala|portaria|recepção|estacionamento|louvor|louvor|multimídia|sonorização)",
    re.IGNORECASE,
)

_QUERY_SCALE_PATTERNS = re.compile(
    r"(quem está|quem está escalado|qual é a escala|próxima escala|quem não confirmou|minha escala)",
    re.IGNORECASE,
)

_QUERY_PENDENCIES_PATTERNS = re.compile(
    r"(o que está pendente|pendências|não foi resolvido|tarefas.*abertas|coisas vencidas|em andamento)",
    re.IGNORECASE,
)

_REGISTER_DECISION_PATTERNS = re.compile(
    r"(registre que|anote que decidimos|decidimos|ficou responsável|ficou responsável pelo)",
    re.IGNORECASE,
)

_CREATE_TASK_PATTERNS = re.compile(
    r"(crie uma tarefa|criar tarefa|tarefa para|atribuir tarefa|precisa ser feito)",
    re.IGNORECASE,
)

_MY_PERMISSIONS_PATTERNS = re.compile(
    r"(o que eu posso|quais são minhas permissões|minhas permissões|meus acessos|o que posso fazer)",
    re.IGNORECASE,
)

_WEEK_SUMMARY_PATTERNS = re.compile(
    r"(prepare a semana|resumo da semana|o que tem esta semana|resumo semanal)",
    re.IGNORECASE,
)

_WHAT_CHANGED_PATTERNS = re.compile(
    r"(o que mudou|o que foi alterado|quais ações|histórico de alterações|resumo de alterações)",
    re.IGNORECASE,
)


# ── Resolução de departamento / grupo WhatsApp ──────────────────────

def _strip_accents(value: str) -> str:
    return "".join(
        ch
        for ch in unicodedata.normalize("NFD", value.lower())
        if unicodedata.category(ch) != "Mn"
    )


def _name_variants(name: str) -> set[str]:
    """Variações do nome do departamento: com/sem plural, sem acentos."""
    n = _strip_accents(name or "")
    out = {n}
    if n.endswith("s"):
        out.add(n[:-1])
    else:
        out.add(n + "s")
    return out


def check_department_scope(user: AuthorizedUser, department_id: int | None, group_jid: str) -> bool:
    """Verifica se o usuário tem permissão para o departamento/grupo pedido.
    Escopos vazios (sem restrição cadastrada) liberam todos os departamentos."""
    depts = _parse_permissions(user.allowed_departments)
    if depts:
        if department_id is None:
            return False
        try:
            allowed_ids = {int(x) for x in depts}
        except (TypeError, ValueError):
            return False
        if int(department_id) not in allowed_ids:
            return False
    groups = _parse_permissions(user.allowed_groups)
    if groups:
        if not group_jid or group_jid not in {str(g) for g in groups}:
            return False
    return True


_ACCENT_CLASSES = {
    "a": "[aáàâã]",
    "e": "[eéê]",
    "i": "[ií]",
    "o": "[oóôõ]",
    "u": "[uúü]",
    "c": "[cç]",
    "n": "[nñ]",
}


def _accent_regex(name: str) -> str:
    """Constrói um regex com acentos flexíveis a partir de um nome."""
    out = []
    for ch in _strip_accents(name):
        out.append(_ACCENT_CLASSES.get(ch, re.escape(ch)))
    return "".join(out)


def _extract_group_reference(text: str):
    """Captura genérica do grupo citado na frase ('o grupo Contatos da Igreja').

    Retorna (nome, posição final da menção) ou None.
    Usado como fallback quando nenhum departamento casa com o texto."""
    m = _GROUP_CLAIM_RE.search(text or "")
    if not m:
        return None
    name = m.group(1).strip()
    if len(name) < 2:
        return None
    return name, m.end()


def _resolve_send_target(db: Session, church_id: int, text: str):
    """Resolve o alvo do envio citado na mensagem (departamento OU nome de grupo).

    Retorna dict:
      {department, department_name, group_name, group_id, message, generic}
    Onde group_id pode ser vazio se o departamento não tiver grupo vinculado;
    nesse caso o executor real busca na lista REAL do WhatsApp pelo nome.

    Nunca inventa vínculo: usa o cadastro de Departamentos e, como fallback,
    apenas o nome do grupo citado textualmente."""
    if not (text or "").strip():
        return None
    verb = _NOTIFY_GROUP_PATTERNS.search(text)
    if not verb:
        return None
    verb_end = verb.end()

    depts = (
        db.query(Department)
        .filter(Department.church_id == church_id, Department.active == True)  # noqa: E712
        .all()
    )
    best = None  # (score, department, start, end)
    for d in depts:
        candidates = []
        if (d.group_name or "").strip():
            candidates.append(d.group_name)
        candidates.append(d.name)
        for raw in candidates:
            for variant in _name_variants(raw):
                if len(_strip_accents(variant)) < 2:
                    continue
                pattern = re.compile(_accent_regex(variant), re.IGNORECASE)
                for m in pattern.finditer(text):
                    idx = m.start()
                    if idx < verb_end or idx - verb_end > _MAX_TARGET_DISTANCE:
                        continue
                    score = m.end() - m.start()
                    before = text[max(0, idx - 28):idx].lower()
                    if re.search(r"(grupo de |grupo do |grupo |departamento de |departamento do |no grupo |no departamento )$", before):
                        score += 18
                    if best is None or score > best[0] or (score == best[0] and idx < best[2]):
                        best = (score, d, m.start(), m.end())

    if best:
        _, department, start, end = best
        message = _message_after(text[end:])
        return {
            "department": department,
            "department_name": department.name,
            "group_name": department.group_name or department.name,
            "group_id": department.group_jid,
            "message": message,
            "generic": False,
        }

    # Fallback: nome do grupo citado textualmente (sem cadastro prévio).
    ref = _extract_group_reference(text)
    if ref:
        name, end = ref
        message = _message_after(text[end:])
        return {
            "department": None,
            "department_name": "",
            "group_name": name,
            "group_id": "",
            "message": message,
            "generic": True,
        }
    return None


# ── Função principal ─────────────────────────────────────────────────

def analyze_admin_command(
    db: Session,
    church_id: int,
    sender_phone: str,
    text: str,
) -> AdminResult:
    """Analisa se o remetente é usuário autorizado e interpreta o comando.

    Retorna AdminResult com intent, dados da ação e resposta.
    """
    phone_digits = "".join(c for c in sender_phone if c.isdigit())
    user = (
        db.query(AuthorizedUser)
        .filter(
            AuthorizedUser.church_id == church_id,
            AuthorizedUser.phone == phone_digits,
            AuthorizedUser.status == "active",
        )
        .first()
    )
    if not user:
        return AdminResult(recognized=False)

    text_clean = text.strip()

    # ── "O que posso fazer?" ────────────────────────────────────────
    if _MY_PERMISSIONS_PATTERNS.search(text_clean):
        perms = get_effective_permissions(user)
        perm_labels = [p.replace("_", " ") for p in perms]
        return AdminResult(
            recognized=True,
            intent="consultar_permissoes",
            reply=f"Suas permissões: {', '.join(perm_labels) if perm_labels else 'nenhuma permissão especial.'}",
        )

    # ── "Aprenda que..." ────────────────────────────────────────────
    m = _LEARN_PATTERNS.search(text_clean)
    if m:
        if not check_permission(user, "ensinar_informacoes"):
            return AdminResult(
                recognized=True,
                intent="ensinar",
                permission_needed="ensinar_informacoes",
                reply="Você não tem permissão para ensinar informações.",
            )
        content = text_clean[m.end():].strip().rstrip(".")
        return AdminResult(
            recognized=True,
            intent="ensinar",
            needs_confirmation=True,
            reply=f"Vou registrar essa informação:\n\n\"{content}\"\n\nConfirma?",
            action_data={"content": content, "source_user": user.name, "source_phone": phone_digits},
        )

    # ── "Corrija..." ────────────────────────────────────────────────
    if _CORRECT_PATTERNS.search(text_clean):
        if not check_permission(user, "corrigir_informacoes"):
            return AdminResult(
                recognized=True,
                intent="corrigir",
                permission_needed="corrigir_informacoes",
                reply="Você não tem permissão para corrigir informações.",
            )
        return AdminResult(
            recognized=True,
            intent="corrigir",
            needs_confirmation=True,
            reply=f"Entendi a correção. Qual informação exatamente devo alterar? Posso consultar o que está cadastrado.",
            action_data={"raw": text_clean},
        )

    # ── "Já enviou?" — consulta ao status REAL da última ação ────────
    if _SEND_STATUS_QUERY_PATTERNS.search(text_clean):
        return AdminResult(
            recognized=True,
            intent="consultar_status_envio",
            reply="Consultando o status real do envio...",
            action_data={"source_user": user.name},
        )

    # ── "Avise o grupo X que Y" ─────────────────────────────────────
    if _NOTIFY_GROUP_PATTERNS.search(text_clean):
        if not check_permission(user, "enviar_avisos"):
            return AdminResult(
                recognized=True,
                intent="enviar_aviso_grupo",
                permission_needed="enviar_avisos",
                reply="Você não tem permissão para enviar avisos.",
            )
        target = _resolve_send_target(db, church_id, text_clean)
        if target is None:
            if _GROUP_REFERENCE_PATTERNS.search(text_clean):
                return AdminResult(
                    recognized=True,
                    intent="enviar_aviso_grupo",
                    reply=(
                        "Para qual grupo devo enviar? Diga o departamento ou o nome do grupo. "
                        'Ex.: "Avise o grupo de Jovens que amanhã tem ensaio."'
                    ),
                    action_data={"source_user": user.name},
                )
            # Sem menção clara de um grupo/departamento: segue o fluxo normal da IA.
            return AdminResult(recognized=False)

        department = target.get("department")
        group_name = target.get("group_name") or ""
        group_id = target.get("group_id") or ""
        message = target.get("message") or ""
        is_draft = bool(_DRAFT_PATTERNS.search(text_clean))

        base = {
            "department_id": department.id if department else None,
            "department": target.get("department_name", ""),
            "group_name": group_name,
            "group_id": group_id,
            "generic": bool(target.get("generic")),
            "source_user": user.name,
        }

        if not group_name:
            return AdminResult(
                recognized=True,
                intent="enviar_aviso_grupo",
                reply="Não identifiquei qual grupo devo avisar.",
                action_data=base,
            )

        # Sem grupo vinculado no cadastro: o executor real tentará achar o
        # JID na lista REAL do WhatsApp pelo nome. Só avisa que "não achou"
        # se essa busca real também falhar (tratado no backend de envio).

        if department and not group_id and not target.get("generic"):
            # Departamento reconhecido mas SEM grupo vinculado: não inventa destino.
            if not _GROUP_REFERENCE_PATTERNS.search(text_clean) and not _extract_group_reference(text_clean):
                return AdminResult(
                    recognized=True,
                    intent="enviar_aviso_grupo",
                    reply=f"O departamento {department.name} ainda não possui um grupo WhatsApp vinculado.",
                    action_data=base,
                )

        if not check_department_scope(user, department.id if department else None, group_id):
            return AdminResult(
                recognized=True,
                intent="enviar_aviso_grupo",
                reply=f"Você não tem permissão para enviar avisos ao grupo {group_name}.",
                action_data=base,
            )

        if not message:
            return AdminResult(
                recognized=True,
                intent="enviar_aviso_grupo",
                reply=f"Qual a mensagem que devo enviar para o grupo \"{group_name}\"?",
                action_data=base,
            )

        base["message"] = message
        base["draft_only"] = is_draft
        preview = message if len(message) <= 160 else message[:157] + "..."
        if is_draft:
            return AdminResult(
                recognized=True,
                intent="enviar_aviso_grupo",
                reply=f"Rascunho pronto para o grupo \"{group_name}\":\n\n\"{preview}\"\n\nMande \"envia\" quando quiser que eu envie.",
                action_data=base,
            )
        return AdminResult(
            recognized=True,
            intent="enviar_aviso_grupo",
            needs_confirmation=is_draft,
            reply=f"Enviando para o grupo \"{group_name}\".",
            action_data=base,
        )

    # ── "Amanhã às cinco avise..." (agendamento) ────────────────────
    if _SCHEDULE_PATTERNS.search(text_clean) and not _NOTIFY_GROUP_PATTERNS.search(text_clean):
        if not check_permission(user, "agendar_avisos"):
            return AdminResult(
                recognized=True,
                intent="agendar_aviso",
                permission_needed="agendar_avisos",
                reply="Você não tem permissão para agendar avisos.",
            )
        return AdminResult(
            recognized=True,
            intent="agendar_aviso",
            needs_confirmation=True,
            reply="Vou agendar esse aviso. Confirma o conteúdo e os destinatários?",
            action_data={"raw": text_clean},
        )

    # ── Escala ──────────────────────────────────────────────────────
    if _SCHEDULE_SCALE_PATTERNS.search(text_clean):
        if not check_permission(user, "criar_escalas"):
            return AdminResult(
                recognized=True,
                intent="criar_escala",
                permission_needed="criar_escalas",
                reply="Você não tem permissão para criar escalas.",
            )
        return AdminResult(
            recognized=True,
            intent="criar_escala",
            needs_confirmation=True,
            reply="Vou criar a escala. Para qual evento e data? Quem deve ser escalado para cada função?",
            action_data={"raw": text_clean},
        )

    if _CHANGE_SCALE_PATTERNS.search(text_clean):
        if not check_permission(user, "alterar_escalas"):
            return AdminResult(
                recognized=True,
                intent="alterar_escala",
                permission_needed="alterar_escalas",
                reply="Você não tem permissão para alterar escalas.",
            )
        return AdminResult(
            recognized=True,
            intent="alterar_escala",
            needs_confirmation=True,
            reply="Vou fazer essa alteração na escala. Para qual evento e data?",
            action_data={"raw": text_clean},
        )

    if _QUERY_SCALE_PATTERNS.search(text_clean):
        if not check_permission(user, "consultar_escalas"):
            return AdminResult(
                recognized=True,
                intent="consultar_escala",
                permission_needed="consultar_escalas",
                reply="Você não tem permissão para consultar escalas.",
            )
        return AdminResult(
            recognized=True,
            intent="consultar_escala",
            reply="Consultando as escalas...",
            action_data={"raw": text_clean},
        )

    # ── Pendências ──────────────────────────────────────────────────
    if _QUERY_PENDENCIES_PATTERNS.search(text_clean):
        if not check_permission(user, "consultar_pendencias"):
            return AdminResult(
                recognized=True,
                intent="consultar_pendencias",
                permission_needed="consultar_pendencias",
                reply="Você não tem permissão para consultar pendências.",
            )
        return AdminResult(
            recognized=True,
            intent="consultar_pendencias",
            reply="Consultando pendências...",
            action_data={"raw": text_clean},
        )

    # ── Decisões ────────────────────────────────────────────────────
    if _REGISTER_DECISION_PATTERNS.search(text_clean):
        if not check_permission(user, "registrar_decisoes"):
            return AdminResult(
                recognized=True,
                intent="registrar_decisao",
                permission_needed="registrar_decisoes",
                reply="Você não tem permissão para registrar decisões.",
            )
        return AdminResult(
            recognized=True,
            intent="registrar_decisao",
            needs_confirmation=True,
            reply="Vou registrar essa decisão. Confirma?",
            action_data={"raw": text_clean, "user_name": user.name},
        )

    # ── Tarefas ─────────────────────────────────────────────────────
    if _CREATE_TASK_PATTERNS.search(text_clean):
        if not check_permission(user, "criar_tarefas"):
            return AdminResult(
                recognized=True,
                intent="criar_tarefa",
                permission_needed="criar_tarefas",
                reply="Você não tem permissão para criar tarefas.",
            )
        return AdminResult(
            recognized=True,
            intent="criar_tarefa",
            needs_confirmation=True,
            reply="Vou criar essa tarefa. Responsável e prazo?",
            action_data={"raw": text_clean, "user_name": user.name},
        )

    # ── O que mudou / Prepare a semana ──────────────────────────────
    if _WHAT_CHANGED_PATTERNS.search(text_clean):
        return AdminResult(
            recognized=True,
            intent="consultar_auditoria",
            reply="Consultando o histórico de alterações...",
            action_data={"raw": text_clean},
        )

    if _WEEK_SUMMARY_PATTERNS.search(text_clean):
        return AdminResult(
            recognized=True,
            intent="resumo_semana",
            reply="Consultando a programação da semana...",
            action_data={"raw": text_clean},
        )

    # ── Comando não reconhecido ─────────────────────────────────────
    return AdminResult(
        recognized=False,
        reply="Não identifiquei um comando administrativo. Posso ajudar com escalas, avisos, informações ou tarefas.",
    )
