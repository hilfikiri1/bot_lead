from datetime import datetime
from sqlalchemy import String, DateTime, ForeignKey, func, Text, BigInteger
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class VoiceNote(Base):
    __tablename__ = "voice_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), nullable=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    audio_url: Mapped[str | None] = mapped_column(Text)
    transcript: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    lead: Mapped["Lead"] = relationship("Lead", back_populates="voice_notes")  # noqa
    ai_report: Mapped["AIReport | None"] = relationship("AIReport", back_populates="voice_note", uselist=False)  # noqa
