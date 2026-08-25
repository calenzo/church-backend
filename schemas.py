from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LlmConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    base_url: str
    model: str
    api_key: str = ""
    temperature: float
    system_prompt: str = ""
    process_text: bool = True
    process_audio: bool = True
    process_groups: bool = True
    process_private: bool = True
    auto_reply: bool = True
    forward_to_groups: bool = True
    apply_routing_rules: bool = True
    auto_register_contacts: bool = True
    auto_memory: bool = True
    updated_at: datetime | None = None


class LlmConfigIn(BaseModel):
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_key: str = ""
    temperature: float = Field(ge=0.0, le=2.0)
    system_prompt: str = ""
    process_text: bool = True
    process_audio: bool = True
    process_groups: bool = True
    process_private: bool = True
    auto_reply: bool = True
    forward_to_groups: bool = True
    apply_routing_rules: bool = True
    auto_register_contacts: bool = True
    auto_memory: bool = True


class DepartmentBase(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""
    group_name: str = ""
    group_jid: str = ""
    active: bool = True


class DepartmentCreate(DepartmentBase):
    pass


class DepartmentUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    group_name: str | None = None
    group_jid: str | None = None
    active: bool | None = None


class DepartmentOut(DepartmentBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime | None = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    direction: str
    from_number: str
    to_jid: str
    text: str
    department_id: int | None
    department_name: str
    llm_reply: str
    status: str
    error: str
    media_key: str = ""
    media_message_id: str = ""
    steps: str = ""
    created_at: datetime | None = None


class ServiceStatus(BaseModel):
    llm: str
    evolution: str
    llm_model: str = ""
    instance: str = ""


class TestSendIn(BaseModel):
    number: str = Field(min_length=1, description="Número/JID de destino")
    text: str = Field(min_length=1)


class PairingCodeIn(BaseModel):
    number: str = Field(min_length=1, description="Número no formato 5511999999999")


class LoginIn(BaseModel):
    email: str = Field(min_length=3)
    password: str = Field(min_length=4)


class UserCreateIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    name: str = Field(default="", max_length=160)
    password: str = Field(min_length=6)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    name: str
    role: str
    church_id: int | None = None
    active: bool = True


class TokenOut(BaseModel):
    token: str
    user: UserOut


class ChurchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class ChurchUpdate(BaseModel):
    name: str | None = None
    active: bool | None = None


class ChurchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    active: bool
    created_at: datetime | None = None


class ContactBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    phone: str = Field(min_length=10, max_length=30, description="Número com DDD, ex.: (21) 99906-9940")
    role: str = Field(default="", max_length=80)


class ContactCreate(ContactBase):
    pass


class ContactUpdate(BaseModel):
    name: str | None = None
    phone: str | None = None
    role: str | None = None
    contact_type: str | None = Field(default=None, max_length=40)
    department_name: str | None = Field(default=None, max_length=120)
    resumo_contexto: str | None = None
    memory_locked: bool | None = None


class ContactOut(ContactBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    church_id: int
    created_at: datetime | None = None
    contact_type: str = ""
    department_name: str = ""
    resumo_contexto: str = ""
    last_intent: str = ""
    last_talk_at: datetime | None = None
    memory_locked: bool = False


class MemoryCreate(BaseModel):
    kind: str = Field(default="observacao", pattern="^(fato|pendencia|observacao)$")
    content: str = Field(min_length=2)
    responsible: str = Field(default="", max_length=120)
    status: str = Field(default="", pattern="^(|aberta|resolvida)$")
    memory_type: str = Field(default="permanente", pattern="^(temporaria|permanente)$")
    expires_at: datetime | None = None


class MemoryUpdate(BaseModel):
    content: str | None = None
    responsible: str | None = Field(default=None, max_length=120)
    status: str | None = Field(default=None, pattern="^(aberta|resolvida|)$")
    memory_type: str | None = Field(default=None, pattern="^(temporaria|permanente)$")
    expires_at: datetime | None = None


class MemoryOut(MemoryCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    church_id: int
    contact_id: int
    source: str = "manual"
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RoutingRuleBase(BaseModel):
    topic: str = Field(min_length=2, max_length=160, description="Assunto/intenção, ex.: Escala da limpeza")
    responsible: str = Field(default="", max_length=160, description="Setor responsável, ex.: Secretaria")
    phone: str = Field(min_length=10, max_length=30, description="Telefone do responsável com DDD")
    department_name: str = Field(default="", max_length=120)
    active: bool = True


class RoutingRuleCreate(RoutingRuleBase):
    pass


class RoutingRuleUpdate(BaseModel):
    topic: str | None = None
    responsible: str | None = None
    phone: str | None = None
    department_name: str | None = None
    active: bool | None = None


class RoutingRuleOut(RoutingRuleBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    church_id: int
    created_at: datetime | None = None


class NumberCreate(BaseModel):
    label: str = ""
    instance_name: str | None = Field(
        default=None, max_length=120,
        description="Opcional. Gerado automaticamente se vazio.",
    )


class NumberUpdate(BaseModel):
    label: str | None = None
    active: bool | None = None


class NumberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    church_id: int
    instance_name: str
    label: str
    phone: str
    active: bool
    created_at: datetime | None = None

# ----------------------------- Membros / aniversários -----------------------------


def _validate_birthday(day: int, month: int) -> None:
    """Dia/mês plausíveis e data real (31/02 é rejeitado)."""
    if not 1 <= day <= 31:
        raise ValueError("Dia deve estar entre 1 e 31")
    if not 1 <= month <= 12:
        raise ValueError("Mês deve estar entre 1 e 12")
    try:
        datetime(2020, month, day)
    except ValueError as exc:
        raise ValueError(f"Data inválida: {day:02d}/{month:02d}") from exc


class MemberCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    birth_day: int = Field(ge=1, le=31)
    birth_month: int = Field(ge=1, le=12)

    @model_validator(mode="after")
    def check_date(self):
        _validate_birthday(self.birth_day, self.birth_month)
        return self


class MemberUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    birth_day: int | None = Field(default=None, ge=1, le=31)
    birth_month: int | None = Field(default=None, ge=1, le=12)


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    birth_day: int
    birth_month: int
    birthday: str = ""  # "dd/mm"
    is_today: bool = False


class RecipientCreate(BaseModel):
    name: str = Field(default="", max_length=160)
    phone: str = Field(min_length=10, max_length=30)


class RecipientUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    phone: str | None = Field(default=None, min_length=10, max_length=30)
    active: bool | None = None


class RecipientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    phone: str
    active: bool


class BirthdayConfigOut(BaseModel):
    send_time: str
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class BirthdayConfigIn(BaseModel):
    send_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class ReminderLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    recipient_name: str
    phone: str
    members_text: str
    message: str
    kind: str
    status: str
    error: str
    ref_date: date
    sent_at: datetime | None
    created_at: datetime | None


class UpcomingBirthday(BaseModel):
    name: str
    birthday: str  # "dd/mm"
    days_until: int
    is_today: bool
