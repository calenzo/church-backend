from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "Church WhatsApp Assistant"
    database_url: str = "sqlite:///./church.db"

    # Evolution API (auto-hospedada)
    evolution_base_url: str = "http://localhost:8080"
    evolution_api_key: str = ""
    evolution_instance: str = "church"
    # Token opcional para validar chamadas do webhook da Evolution
    webhook_token: str = ""

    # LLM (API online compatível com OpenAI)
    llm_base_url: str = "http://localhost:11434"
    llm_model: str = "llama3.1"
    llm_api_key: str = ""
    llm_temperature: float = 0.3
    llm_timeout: float = 120.0

    # Super admin criado automaticamente no primeiro boot (troque a senha!)
    admin_email: str = "admin@igreja.local"
    admin_password: str = "mudar123"
    admin_name: str = "Super Admin"

    cors_origins: str = "*"


settings = Settings()
