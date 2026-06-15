"""
server.py
─────────
FastAPI application factory.

On startup (lifespan):
  1. Create the application DB tables (app_users / app_chat_histories /
     app_chat_messages).
  2. Open the agent persistence layer (AsyncPostgresStore + checkpointer) and
     keep the connection pools open for the app's lifetime.
  3. Build the main canvas/coding agent and stash it in app.state.agent_cache.

Routers: auth, history (chat list/CRUD), chat (streaming).
"""

import asyncio
import sys

# ── Windows event-loop fix (must run before any event loop is created) ───────
# psycopg's async mode is incompatible with Windows' default ProactorEventLoop.
# Switch to the SelectorEventLoop policy so AsyncPostgresStore/Saver work.
# This module is imported before uvicorn creates its loop, so setting the
# policy here applies whether launched via `uvicorn app.server:app` or main.py.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db import database, models
from app.agents.main_agent.agent import create_main_agent
from app.agents.shared.agent_contexts import Context
from app.agents.shared.llms import get_llm
from app.agents.shared.memory import open_agent_persistence
from app.api.routers import auth as auth_router
from app.api.routers import history as history_router
from app.api.routers import chat as chat_router
from app.api.routers import admin as admin_router
from app.api.routers import models as models_router
from app.core import auth as auth_utils
from app import models_config
from app.settings import settings
from app.logger import get_logger

logger = get_logger(__name__)


def _ensure_default_admin():
    """Create the default admin user from .env if it doesn't exist."""
    from app.db.database import SessionLocal
    
    with SessionLocal() as db:
        admin = db.query(models.User).filter(
            models.User.username == settings.ADMIN_USERNAME
        ).first()
        
        if admin is None:
            logger.info("Creating default admin user: %s", settings.ADMIN_USERNAME)
            hashed_password = auth_utils.get_password_hash(settings.ADMIN_PASSWORD)
            admin = models.User(
                username=settings.ADMIN_USERNAME,
                hashed_password=hashed_password,
                role="admin",
                is_approved=True,
                token_quota=-1,  # Unlimited for admin
                tokens_used_this_month=0,
            )
            db.add(admin)
            db.commit()
            logger.info("Default admin user created successfully.")
        else:
            # Ensure the configured administrator always has admin privileges
            # and unlimited monthly quota.
            if admin.role != "admin" or not admin.is_approved or admin.token_quota != -1:
                admin.role = "admin"
                admin.is_approved = True
                admin.token_quota = -1
                db.commit()
                logger.info("Updated existing user %s to admin role.", settings.ADMIN_USERNAME)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Startup: creating application DB tables.")
    models.Base.metadata.create_all(bind=database.engine)
    
    logger.info("Startup: ensuring default admin user exists.")
    _ensure_default_admin()

    logger.info("Startup: opening agent persistence + building main agent.")
    async with open_agent_persistence(settings.STORE_DATABASE_URL) as (store, checkpointer):
        app.state.store = store
        app.state.checkpointer = checkpointer
        app.state.agent_cache_lock = asyncio.Lock()
        agent = await create_main_agent(
            llm=get_llm(),
            context_schema=Context,
            store=store,
            checkpointer=checkpointer,
        )
        app.state.agent_cache = {models_config.get_default_model(): agent}
        logger.info("Startup complete. Main agent ready.")
        yield
        logger.info("Shutdown: closing agent persistence.")


app = FastAPI(
    title="Advanced LangChain ChatBot API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-History-Id"],
)

app.include_router(auth_router.router)
app.include_router(history_router.router)
app.include_router(chat_router.router)
app.include_router(admin_router.router)
app.include_router(models_router.router)


@app.get("/health")
async def health():
    return {"status": "ok"}