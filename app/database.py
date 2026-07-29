from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

# Never print DATABASE_URL: it contains the PostgreSQL password.
engine = create_async_engine(
    settings.database_url,
    echo=settings.app_env == "development",
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db():
    """Create all tables in development/tests; production uses Alembic."""
    from app.models import (  # noqa
        action, agent_message, agent_session, ai_report, calendar_event, client,
        integration_check, integration_event, lead, pending_agent_action,
        spreadsheet_lead_mapping, voice_note,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
