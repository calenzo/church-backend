from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class LLMConfig(Base):
    """Configuração única (id=1) da LLM e do contato principal da igreja."""

    __tablename__ = "llm_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
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
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    group_name: Mapped[str] = mapped_column(String(160), default="")
    group_jid: Mapped[str] = mapped_column(String(120), default="")
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    messages: Mapped[list["MessageLog"]] = relationship(back_populates="department")


class MessageLog(Base):
    """Log de todas as mensagens processadas."""

    __tablename__ = "message_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    direction: Mapped[str] = mapped_column(String(10))  # "in" | "out"
    from_number: Mapped[str] = mapped_column(String(40), default="")
    to_jid: Mapped[str] = mapped_column(String(120), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"), nullable=True)
    department_name: Mapped[str] = mapped_column(String(120), default="")
    llm_reply: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), default="received")  # received|routed|failed|sent
    error: Mapped[str] = mapped_column(Text, default="")
    steps: Mapped[str] = mapped_column(Text, default="")  # JSON: [{"step","status","detail","ts"}]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    department: Mapped[Department | None] = relationship(back_populates="messages")
