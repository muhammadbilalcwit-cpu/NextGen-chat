"""
SQLAlchemy model matching the practice project's users table.
Read-only — chat microservice never writes to this table.
"""
from sqlalchemy import BigInteger, String, Integer, DateTime, Boolean, Text, Column
from sqlalchemy.dialects.postgresql import JSONB

from app.database.postgres import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), nullable=False, unique=True)
    password = Column(String(255), nullable=False)
    firstname = Column(Text, nullable=True)
    lastname = Column(Text, nullable=True)
    age = Column(Integer, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    is_deleted = Column(Boolean, nullable=True, default=False)
    profile_picture = Column(String(500), nullable=True)
    created_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)
    deactivated_at = Column(DateTime, nullable=True)
    deleted_at = Column(DateTime, nullable=True)
    company_id = Column(BigInteger, nullable=True)
    department_id = Column(BigInteger, nullable=True)
    role_id = Column(BigInteger, nullable=True)

    @property
    def name(self) -> str:
        """Combine firstname and lastname for display."""
        parts = [self.firstname or "", self.lastname or ""]
        return " ".join(p for p in parts if p).strip() or self.email

    @property
    def picture(self) -> str | None:
        return self.profile_picture
