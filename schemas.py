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
