"""
SQLAlchemy model for the customers table.

Fully independent from the users table — customers have their own
email and name columns. Managed (CRUD) by the chat microservice.
"""
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.database.postgres import Base


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("email", "company_id", name="uq_customers_email_company"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), nullable=False)
    name = Column(String(255), nullable=False)
    company_id = Column(Integer, nullable=False)
    phone = Column(String(20), nullable=True)
    location = Column(String(255), nullable=True)
    ip_address = Column(String(45), nullable=True)
    source = Column(String(50), nullable=False, default="widget")
    can_login = Column(Boolean, nullable=False, default=False)
    metadata_ = Column("metadata", JSONB, nullable=False, server_default="{}")
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
