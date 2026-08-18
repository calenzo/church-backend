from contextlib import asynccontextmanager
import logging
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from config import settings
from database import Base, engine
from routers import board, webhook

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    # Migração idempotente para colunas adicionadas depois do primeiro deploy.
    for column in ("steps TEXT DEFAULT ''", "media_key TEXT DEFAULT ''", "media_message_id TEXT DEFAULT ''"):
        try:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE message_log ADD COLUMN IF NOT EXISTS {column}"))
        except Exception:
            pass
    logger.info("Backend iniciado com sucesso")
    yield
    logger.info("Backend desligando (SIGTERM recebido)")
    engine.dispose()


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


@app.get("/")
def root():
    return {"app": settings.app_name, "docs": "/docs", "webhook": "/webhook/evolution"}
