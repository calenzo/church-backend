from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class LlmConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    base_url: str
    model: str
    api_key: str = ""
    temperature: float
    system_prompt: str = ""
    updated_at: datetime | None = None


class LlmConfigIn(BaseModel):
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_key: str = ""
    temperature: float = Field(ge=0.0, le=2.0)
    system_prompt: str = ""


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
