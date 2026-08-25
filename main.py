from contextlib import asynccontextmanager
import asyncio
import logging
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from config import settings
from database import Base, SessionLocal, engine
from routers import board, membros, webhook
from services import evolution as evolution_service
from services.birthday_scheduler import run_loop as birthday_loop
from services.birthday_seed import seed_birthday_data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def _seed_multi_tenant() -> None:
    """Cria igreja padrão, mapeia a instância legada e o super admin no primeiro boot."""
    from auth import hash_password
    from models import Church, Department, LLMConfig, MessageLog, User, WhatsAppNumber

    with SessionLocal() as db:
        church = db.query(Church).order_by(Church.id).first()
        if not church:
            church = Church(name="Igreja Principal", slug="principal")
            db.add(church)
            db.commit()
            db.refresh(church)
            logger.info("Igreja padrão criada (id=%s)", church.id)

        # Vincula registros legados sem igreja à igreja padrão
        db.query(Department).filter(Department.church_id.is_(None)).update({"church_id": church.id})
        db.query(MessageLog).filter(MessageLog.church_id.is_(None)).update({"church_id": church.id})
        db.query(LLMConfig).filter(LLMConfig.church_id.is_(None)).update({"church_id": church.id})

        # Mapeia a instância configurada no .env para a igreja padrão
        if settings.evolution_instance and not (
            db.query(WhatsAppNumber).filter(WhatsAppNumber.instance_name == settings.evolution_instance).first()
        ):
            db.add(
                WhatsAppNumber(
                    church_id=church.id,
                    instance_name=settings.evolution_instance,
                    label="Número principal",
                )
            )

        if not db.query(User).first():
            db.add(
                User(
                    email=settings.admin_email.strip().lower(),
                    name=settings.admin_name,
                    password_hash=hash_password(settings.admin_password),
                    role="super_admin",
                )
            )
            logger.info("Super admin criado: %s", settings.admin_email)

        db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    # Migração idempotente para colunas adicionadas depois do primeiro deploy.
    migrations = [
        "ALTER TABLE message_log ADD COLUMN IF NOT EXISTS church_id INTEGER",
        "ALTER TABLE departments ADD COLUMN IF NOT EXISTS church_id INTEGER",
        "ALTER TABLE llm_config ADD COLUMN IF NOT EXISTS church_id INTEGER",
        "ALTER TABLE message_log ADD COLUMN IF NOT EXISTS steps TEXT DEFAULT ''",
        "ALTER TABLE message_log ADD COLUMN IF NOT EXISTS media_key TEXT DEFAULT ''",
        "ALTER TABLE message_log ADD COLUMN IF NOT EXISTS media_message_id TEXT DEFAULT ''",
        "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS contact_type VARCHAR(40) DEFAULT ''",
        "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS department_name VARCHAR(120) DEFAULT ''",
        "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS resumo_contexto TEXT DEFAULT ''",
        "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS last_intent VARCHAR(160) DEFAULT ''",
        "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS last_talk_at TIMESTAMP",
        "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS memory_locked BOOLEAN DEFAULT FALSE",
        "ALTER TABLE llm_config ADD COLUMN IF NOT EXISTS process_text BOOLEAN DEFAULT TRUE",
        "ALTER TABLE llm_config ADD COLUMN IF NOT EXISTS process_audio BOOLEAN DEFAULT TRUE",
        "ALTER TABLE llm_config ADD COLUMN IF NOT EXISTS process_groups BOOLEAN DEFAULT TRUE",
        "ALTER TABLE llm_config ADD COLUMN IF NOT EXISTS process_private BOOLEAN DEFAULT TRUE",
        "ALTER TABLE llm_config ADD COLUMN IF NOT EXISTS auto_reply BOOLEAN DEFAULT TRUE",
        "ALTER TABLE llm_config ADD COLUMN IF NOT EXISTS forward_to_groups BOOLEAN DEFAULT TRUE",
        "ALTER TABLE llm_config ADD COLUMN IF NOT EXISTS apply_routing_rules BOOLEAN DEFAULT TRUE",
        "ALTER TABLE llm_config ADD COLUMN IF NOT EXISTS auto_register_contacts BOOLEAN DEFAULT TRUE",
        "ALTER TABLE llm_config ADD COLUMN IF NOT EXISTS auto_memory BOOLEAN DEFAULT TRUE",
    ]
    for statement in migrations:
        try:
            with engine.begin() as conn:
                conn.execute(text(statement))
        except Exception:
            pass  # SQLite não suporta IF NOT EXISTS; colunas já nascem pelo create_all
    _seed_multi_tenant()
    seed_birthday_data()  # carga inicial de membros: uma única vez por igreja
    birthday_task = asyncio.create_task(birthday_loop())
    logger.info("Backend iniciado com sucesso")
    yield
    birthday_task.cancel()
    logger.info("Backend desligando (SIGTERM recebido)")
    engine.dispose()
    evolution_service.invalidate_groups_cache()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins if origins != ["*"] else ["*"],
    allow_credentials=origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhook.router)
app.include_router(board.router)
app.include_router(membros.router)


@app.get("/")
def root():
    return {"app": settings.app_name, "docs": "/docs", "webhook": "/webhook/evolution"}
