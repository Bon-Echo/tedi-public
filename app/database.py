from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

def _engine_kwargs() -> dict:
    """Per-driver engine kwargs.

    SQLite (used in tests via aiosqlite) does not accept pool_size /
    max_overflow when running on the default StaticPool, so we omit them.
    """
    kw: dict = {"echo": settings.DEBUG}
    if not settings.DATABASE_URL.startswith("sqlite"):
        kw["pool_size"] = settings.DATABASE_POOL_SIZE
        kw["max_overflow"] = settings.DATABASE_MAX_OVERFLOW
    return kw


engine = create_async_engine(settings.DATABASE_URL, **_engine_kwargs())

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def init_db() -> None:
    """Initialize database connection pool."""
    # Connection is established lazily; this triggers pool warmup.
    async with engine.connect():
        pass


async def close_db() -> None:
    """Dispose the engine connection pool."""
    await engine.dispose()


async def get_session() -> AsyncSession:
    """Dependency: yield an async database session."""
    async with async_session_factory() as session:
        yield session
