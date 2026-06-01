from datetime import datetime
from sqlalchemy import String, DateTime, ForeignKey, func, JSON, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class Action(Base):
    __tablename__ = "actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), nullable=True)
    action_type: Mapped[str] = mapped_column(String(100))   # gmail_draft | calendar_event | whatsapp | crm_save
    status: Mapped[str] = mapped_column(String(50), default="pending")  # pending | approved | executed | failed | cancelled
    payload: Mapped[dict | None] = mapped_column(JSON)
    approved_by_user: Mapped[bool] = mapped_column(Boolean, default=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    lead: Mapped["Lead"] = relationship("Lead", back_populates="actions")  # noqa
