"""
SQLAlchemy model matching the REAL production user_sessions table.
Read-only — used to validate JWT session_id is still active.
"""
from sqlalchemy import BigInteger, String, Integer, Text, Column, DateTime, Numeric
from sqlalchemy.dialects.postgresql import CHAR

from app.database.postgres import Base


class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(CHAR(36), primary_key=True)  # UUID char(36)
    user_id = Column(BigInteger, nullable=False)
    created_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    revoked_by = Column(BigInteger, nullable=True)
    ip = Column(String(45), nullable=True)
    user_agent = Column(Text, nullable=True)
    lat = Column(Numeric(10, 8), nullable=True)
    lon = Column(Numeric(10, 8), nullable=True)
    accuracy = Column(Integer, nullable=True)
