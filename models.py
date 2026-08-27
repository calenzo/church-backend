from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class Church(Base):
    """Igreja (tenant). Cada igreja tem seus departamentos, números e configuração."""

    __tablename__ = "churches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    slug: Mapped[str] = mapped_column(String(160), unique=True)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    departments: Mapped[list["Department"]] = relationship(back_populates="church")
    numbers: Mapped[list["WhatsAppNumber"]] = relationship(back_populates="church")


class User(Base):
    """Usuário do painel. role: 'super_admin' (plataforma) ou 'admin' (da igreja)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(160), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="admin")
    church_id: Mapped[int | None] = mapped_column(ForeignKey("churches.id"), nullable=True)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuthSession(Base):
    """Sessão de login (token opaco enviado no header x-auth-token)."""

    __tablename__ = "auth_sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class WhatsAppNumber(Base):
    """Número do WhatsApp de uma igreja = uma instância na Evolution API."""

    __tablename__ = "whatsapp_numbers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    church_id: Mapped[int] = mapped_column(ForeignKey("churches.id"))
    instance_name: Mapped[str] = mapped_column(String(120), unique=True)
    label: Mapped[str] = mapped_column(String(160), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    church: Mapped[Church] = relationship(back_populates="numbers")


class LLMConfig(Base):
    """Configuração da LLM por igreja."""

    __tablename__ = "llm_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    church_id: Mapped[int | None] = mapped_column(ForeignKey("churches.id"), nullable=True, index=True)
    base_url: Mapped[str] = mapped_column(String(255), default="http://localhost:11434")
    model: Mapped[str] = mapped_column(String(120), default="llama3.1")
    api_key: Mapped[str] = mapped_column(String(255), default="")
    temperature: Mapped[float] = mapped_column(Float, default=0.3)
    system_prompt: Mapped[str] = mapped_column(Text, default="")

    # Permissões do webhook — toggles que controlam o comportamento do processamento
    process_text: Mapped[bool] = mapped_column(default=True)
    process_audio: Mapped[bool] = mapped_column(default=True)
    process_groups: Mapped[bool] = mapped_column(default=True)
    process_private: Mapped[bool] = mapped_column(default=True)
    auto_reply: Mapped[bool] = mapped_column(default=True)
    forward_to_groups: Mapped[bool] = mapped_column(default=True)
    apply_routing_rules: Mapped[bool] = mapped_column(default=True)
    auto_register_contacts: Mapped[bool] = mapped_column(default=True)
    auto_memory: Mapped[bool] = mapped_column(default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Department(Base):
    """Departamento da igreja e o grupo do WhatsApp associado a ele."""

    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    church_id: Mapped[int | None] = mapped_column(ForeignKey("churches.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    group_name: Mapped[str] = mapped_column(String(160), default="")
    group_jid: Mapped[str] = mapped_column(String(120), default="")
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    church: Mapped[Church | None] = relationship(back_populates="departments")
    messages: Mapped[list["MessageLog"]] = relationship(back_populates="department")


class Contact(Base):
    """Contato da igreja: número -> nome/cargo, usado pela IA para reconhecer o remetente."""

    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("church_id", "phone", name="uq_contacts_church_phone"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    church_id: Mapped[int] = mapped_column(ForeignKey("churches.id"), index=True)
    phone: Mapped[str] = mapped_column(String(20))  # apenas dígitos, ex.: 5521999069940
    name: Mapped[str] = mapped_column(String(160))
    role: Mapped[str] = mapped_column(String(80), default="")  # cargo/função, ex.: Pastor
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # ---- ficha estendida / memória inteligente ----
    contact_type: Mapped[str] = mapped_column(String(40), default="")  # Membro, Visitante...
    department_name: Mapped[str] = mapped_column(String(120), default="")
    resumo_contexto: Mapped[str] = mapped_column(Text, default="")
    last_intent: Mapped[str] = mapped_column(String(160), default="")
    last_talk_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    memory_locked: Mapped[bool] = mapped_column(default=False)  # bloqueia escrita automática


class ContactMemory(Base):
    """Memória por contato: fatos úteis, observações e pedidos pendentes.
    Manual (administrador) tem prioridade sobre automático (IA); nunca sobrescreve cadastro."""

    __tablename__ = "contact_memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    church_id: Mapped[int] = mapped_column(ForeignKey("churches.id"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="fato")  # fato | pendencia | observacao
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="")  # pendencia: aberta | resolvida
    responsible: Mapped[str] = mapped_column(String(120), default="")
    memory_type: Mapped[str] = mapped_column(String(20), default="permanente")  # temporaria|permanente
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # automatica | manual
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RoutingRule(Base):
    """Regra de encaminhamento automático: assunto/intenção -> responsável (telefone).
    A IA decide quando usá-la; o usuário só cadastra o destino uma vez."""

    __tablename__ = "routing_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    church_id: Mapped[int] = mapped_column(ForeignKey("churches.id"), index=True)
    topic: Mapped[str] = mapped_column(String(160))  # assunto/intenção, ex.: "Escala da limpeza"
    responsible: Mapped[str] = mapped_column(String(160), default="")  # setor/nome, ex.: Secretaria
    phone: Mapped[str] = mapped_column(String(40), default="")  # telefone do responsável (dígitos)
    department_name: Mapped[str] = mapped_column(String(120), default="")
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MessageLog(Base):
    """Log de todas as mensagens processadas."""

    __tablename__ = "message_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    church_id: Mapped[int | None] = mapped_column(ForeignKey("churches.id"), nullable=True, index=True)
    direction: Mapped[str] = mapped_column(String(10))  # "in" | "out"
    from_number: Mapped[str] = mapped_column(String(40), default="")
    to_jid: Mapped[str] = mapped_column(String(120), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"), nullable=True)
    department_name: Mapped[str] = mapped_column(String(120), default="")
    llm_reply: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), default="received")  # received|routed|failed|sent
    error: Mapped[str] = mapped_column(Text, default="")
    media_key: Mapped[str] = mapped_column(String(200), default="")
    media_message_id: Mapped[str] = mapped_column(String(200), default="")
    steps: Mapped[str] = mapped_column(Text, default="")  # JSON: [{"step","status","detail","ts"}]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    department: Mapped[Department | None] = relationship(back_populates="messages")


# ----------------------------- Membros / aniversários -----------------------------


class Member(Base):
    """Membro da igreja para lembretes de aniversário (dia + mês, sem ano)."""

    __tablename__ = "members"
    __table_args__ = (
        UniqueConstraint("church_id", "name", "birth_day", "birth_month", name="uq_members_unique"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    church_id: Mapped[int] = mapped_column(ForeignKey("churches.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    birth_day: Mapped[int] = mapped_column(Integer)  # 1..31
    birth_month: Mapped[int] = mapped_column(Integer)  # 1..12
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BirthdayRecipient(Base):
    """Destinatário configurável dos lembretes de aniversário."""

    __tablename__ = "birthday_recipients"
    __table_args__ = (
        UniqueConstraint("church_id", "phone", name="uq_birthday_recipients_phone"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    church_id: Mapped[int] = mapped_column(ForeignKey("churches.id"), index=True)
    name: Mapped[str] = mapped_column(String(160), default="")
    phone: Mapped[str] = mapped_column(String(20))  # dígitos normalizados
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BirthdayConfig(Base):
    """Configuração do lembrete por igreja (horário de envio, fuso America/Sao_Paulo)."""

    __tablename__ = "birthday_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    church_id: Mapped[int] = mapped_column(ForeignKey("churches.id"), unique=True, index=True)
    send_time: Mapped[str] = mapped_column(String(5), default="08:00")  # "HH:MM"
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BirthdayLog(Base):
    """Histórico + controle antidualidade: 1 linha por (igreja, data, destinatário).
    A chave ref_date (ano-mês-dia) garante reenvio no próximo ano sem duplicar no mesmo dia.
    kind: 'aniversario' (real) ou 'teste' (botão da aba). status: pendente|enviado|falhou."""

    __tablename__ = "birthday_logs"
    __table_args__ = (
        UniqueConstraint("church_id", "ref_date", "recipient_id", name="uq_birthday_once"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    church_id: Mapped[int] = mapped_column(ForeignKey("churches.id"), index=True)
    recipient_id: Mapped[int | None] = mapped_column(ForeignKey("birthday_recipients.id"), nullable=True)
    recipient_name: Mapped[str] = mapped_column(String(160), default="")
    phone: Mapped[str] = mapped_column(String(20), default="")
    members_text: Mapped[str] = mapped_column(Text, default="")  # nomes dos aniversariantes
    message: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(20), default="aniversario")
    status: Mapped[str] = mapped_column(String(20), default="pendente")
    error: Mapped[str] = mapped_column(Text, default="")
    ref_date: Mapped[date] = mapped_column(Date, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BirthdaySeedFlag(Base):
    """Marca que a carga inicial de membros/destinatários já rodou para a igreja.
    Garante seed único: reinícios NUNCA duplicam os registros iniciais."""

    __tablename__ = "birthday_seed_flags"

    church_id: Mapped[int] = mapped_column(ForeignKey("churches.id"), primary_key=True)
    seeded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SafetyLog(Base):
    """Log de eventos de segurança: proteção, pausa, erros, reconexões."""

    __tablename__ = "safety_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    church_id: Mapped[int] = mapped_column(ForeignKey("churches.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(40), default="info")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuthorizedUser(Base):
    """Usuários autorizados a executar comandos administrativos via WhatsApp."""

    __tablename__ = "authorized_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    church_id: Mapped[int] = mapped_column(ForeignKey("churches.id"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active / blocked
    profile: Mapped[str] = mapped_column(String(40), default="operador")  # administrador / secretaria / lider / operador / comunicador / instrutor
    allowed_departments: Mapped[str] = mapped_column(Text, default="")  # JSON list of dept IDs
    allowed_groups: Mapped[str] = mapped_column(Text, default="")  # JSON list of group JIDs
    permissions: Mapped[str] = mapped_column(Text, default="")  # JSON list of permission keys
    notes: Mapped[str] = mapped_column(Text, default="")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AdminAction(Base):
    """Log de ações administrativas executadas via WhatsApp."""

    __tablename__ = "admin_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    church_id: Mapped[int] = mapped_column(ForeignKey("churches.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("authorized_users.id"), nullable=True)
    user_name: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(20), default="")
    raw_command: Mapped[str] = mapped_column(Text, default="")
    intent: Mapped[str] = mapped_column(String(60), default="")
    action: Mapped[str] = mapped_column(Text, default="")
    target: Mapped[str] = mapped_column(Text, default="")
    previous_value: Mapped[str] = mapped_column(Text, default="")
    new_value: Mapped[str] = mapped_column(Text, default="")
    department: Mapped[str] = mapped_column(String(120), default="")
    group_jid: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(30), default="recebido")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ChurchKnowledge(Base):
    """Informações ensinadas por usuários autorizados via WhatsApp."""

    __tablename__ = "church_knowledge"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    church_id: Mapped[int] = mapped_column(ForeignKey("churches.id"), index=True)
    category: Mapped[str] = mapped_column(String(40), default="institucional")
    key_topic: Mapped[str] = mapped_column(String(200), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    department: Mapped[str] = mapped_column(String(120), default="")
    source_user: Mapped[str] = mapped_column(String(120), default="")
    source_phone: Mapped[str] = mapped_column(String(20), default="")
    valid_from: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
