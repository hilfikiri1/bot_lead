from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.action import Action
    from app.models.client import Client
    from app.models.voice_note import VoiceNote


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("clients.id"), nullable=True
    )
    kommo_lead_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, unique=True, index=True
    )
    kommo_pipeline_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    kommo_status_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    kommo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    product_requested: Mapped[str | None] = mapped_column(Text)
    budget: Mapped[str | None] = mapped_column(String(255))
    country: Mapped[str | None] = mapped_column(String(100))
    city: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(50), default="new")
    priority: Mapped[str] = mapped_column(String(20), default="medium")
    next_action: Mapped[str | None] = mapped_column(Text)
    next_followup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    client: Mapped["Client"] = relationship("Client", back_populates="leads")  # noqa
    voice_notes: Mapped[list["VoiceNote"]] = relationship(
        "VoiceNote", back_populates="lead"
    )  # noqa
    actions: Mapped[list["Action"]] = relationship("Action", back_populates="lead")  # noqa
