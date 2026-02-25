from sqlalchemy import text
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
    """Test PostgreSQL connection on startup and ensure chat-managed tables exist."""
    async with engine.begin() as conn:
        # Test connectivity
        await conn.execute(text("SELECT 1"))

        # Check if customers table exists with old schema (has user_id column)
        has_user_id = await conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'customers' AND column_name = 'user_id'
        """))

        if has_user_id.first():
            # Migrate: add new columns, copy data from users, drop user_id
            await conn.execute(text("""
                ALTER TABLE customers ADD COLUMN IF NOT EXISTS email VARCHAR(255)
            """))
            await conn.execute(text("""
                ALTER TABLE customers ADD COLUMN IF NOT EXISTS name VARCHAR(255)
            """))
            await conn.execute(text("""
                UPDATE customers c
                SET email = u.email,
                    name = COALESCE(
                        NULLIF(TRIM(COALESCE(u.firstname, '') || ' ' || COALESCE(u.lastname, '')), ''),
                        u.email
                    )
                FROM users u
                WHERE c.user_id = u.id AND c.email IS NULL
            """))
            # Set defaults for any rows that couldn't be migrated
            await conn.execute(text("""
                UPDATE customers SET email = 'unknown@migrated.local', name = 'Migrated'
                WHERE email IS NULL
            """))
            await conn.execute(text("""
                ALTER TABLE customers ALTER COLUMN email SET NOT NULL
            """))
            await conn.execute(text("""
                ALTER TABLE customers ALTER COLUMN name SET NOT NULL
            """))
            await conn.execute(text("""
                ALTER TABLE customers DROP CONSTRAINT IF EXISTS customers_user_id_fkey
            """))
            await conn.execute(text("""
                ALTER TABLE customers DROP CONSTRAINT IF EXISTS customers_user_id_key
            """))
            await conn.execute(text("""
                ALTER TABLE customers DROP COLUMN IF EXISTS user_id
            """))
            # Add unique constraint (email, company_id) if not exists
            await conn.execute(text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'uq_customers_email_company'
                    ) THEN
                        ALTER TABLE customers ADD CONSTRAINT uq_customers_email_company
                        UNIQUE (email, company_id);
                    END IF;
                END $$
            """))
        else:
            # Fresh install or already migrated — create table with new schema
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS customers (
                    id SERIAL PRIMARY KEY,
                    email VARCHAR(255) NOT NULL,
                    name VARCHAR(255) NOT NULL,
                    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                    phone VARCHAR(20),
                    location VARCHAR(255),
                    ip_address VARCHAR(45),
                    source VARCHAR(50) NOT NULL DEFAULT 'widget',
                    can_login BOOLEAN NOT NULL DEFAULT false,
                    metadata JSONB NOT NULL DEFAULT '{}',
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_customers_email_company UNIQUE (email, company_id)
                )
            """))


async def close_postgres():
    await engine.dispose()
