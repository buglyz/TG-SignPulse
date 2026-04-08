from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint

from backend.core.database import Base


class PersistedState(Base):
    __tablename__ = "persisted_states"
    __table_args__ = (
        UniqueConstraint("category", "item_key", "scope", name="uq_persisted_state"),
    )

    id = Column(Integer, primary_key=True, index=True)
    category = Column(String(64), nullable=False, index=True)
    item_key = Column(String(128), nullable=False, index=True)
    scope = Column(String(128), nullable=False, default="", index=True)
    payload = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
