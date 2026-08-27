"""Secretária Inteligente — comandos administrativos via WhatsApp.

Detecta usuários autorizados, interpreta comandos em linguagem natural,
valida permissões e retorna ações estruturadas para o backend executar.
A IA NÃO modifica o banco diretamente — gera intents que o backend valida.
"""

import json
import logging
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from models import AuthorizedUser

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
    r"(avise|avis[ae]|envie|manda|envia|comunique|informe|comunica).*(grupo|group|equipe|time|lideranças?|irmãs?|irmãos?|jovens?|crianças?|mulheres?|homens?|adultos?|escola bíblica|ebd|oração|intercessão)",
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

    # ── "Avise o grupo..." ──────────────────────────────────────────
    if _NOTIFY_GROUP_PATTERNS.search(text_clean):
        if not check_permission(user, "enviar_avisos"):
            return AdminResult(
                recognized=True,
                intent="enviar_aviso",
                permission_needed="enviar_avisos",
                reply="Você não tem permissão para enviar avisos.",
            )
        is_draft = bool(_DRAFT_PATTERNS.search(text_clean))
        is_exact = bool(_EXACT_SEND_PATTERNS.search(text_clean))
        is_improve = bool(_IMPROVE_SEND_PATTERNS.search(text_clean))
        return AdminResult(
            recognized=True,
            intent="enviar_aviso",
            needs_confirmation=not is_draft,
            reply=(
                "Vou preparar o aviso. Qual grupo exatamente devo enviar?"
                if not is_draft
                else "Rascunho pronto. Quer que eu envie ou prefere revisar?"
            ),
            action_data={
                "raw": text_clean,
                "draft_only": is_draft,
                "exact_text": is_exact,
                "improve_before": is_improve,
            },
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
