from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=settings.DATABASE_POOL_SIZE,
    max_overflow=settings.DATABASE_MAX_OVERFLOW,
    echo=settings.DEBUG,
)

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
