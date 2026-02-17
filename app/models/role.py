"""
SQLAlchemy models for roles and user_roles tables.
Read-only — chat microservice never writes to these tables.
"""
from sqlalchemy import Integer, String, DateTime, Column, ForeignKey

from app.database.postgres import Base


class Role(Base):
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True)
    slug = Column(String(50), nullable=False, unique=True)
    name = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=True)
    update_at = Column(DateTime, nullable=True)


class UserRole(Base):
    __tablename__ = "user_roles"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role_id = Column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False)
