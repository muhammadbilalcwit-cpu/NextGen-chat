from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(settings.POSTGRES_URL, echo=False, pool_size=5, max_overflow=10)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    """FastAPI dependency that yields an async PostgreSQL session."""
    async with async_session() as session:
        yield session


async def init_postgres():
    """Test PostgreSQL connection on startup."""
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: None)  # just test connectivity


async def close_postgres():
    await engine.dispose()
