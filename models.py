from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
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
