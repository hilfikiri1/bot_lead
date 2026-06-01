from datetime import datetime
from sqlalchemy import String, DateTime, ForeignKey, func, Text, Float, Boolean, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class AIReport(Base):
    __tablename__ = "ai_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    voice_note_id: Mapped[int] = mapped_column(ForeignKey("voice_notes.id"))
    conversation_summary: Mapped[str | None] = mapped_column(Text)
    what_manager_said: Mapped[list | None] = mapped_column(JSON)
    mistakes_or_weak_points: Mapped[list | None] = mapped_column(JSON)
    missing_questions: Mapped[list | None] = mapped_column(JSON)
    recommended_next_step: Mapped[str | None] = mapped_column(Text)
    email_subject: Mapped[str | None] = mapped_column(String(500))
    email_body: Mapped[str | None] = mapped_column(Text)
    whatsapp_message: Mapped[str | None] = mapped_column(Text)
    calendar_title: Mapped[str | None] = mapped_column(String(500))
    calendar_description: Mapped[str | None] = mapped_column(Text)
    calendar_start_time: Mapped[str | None] = mapped_column(String(50))
    calendar_duration_minutes: Mapped[int | None] = mapped_column(default=15)
    confidence_score: Mapped[float | None] = mapped_column(Float)
    needs_human_review: Mapped[bool] = mapped_column(Boolean, default=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    voice_note: Mapped["VoiceNote"] = relationship("VoiceNote", back_populates="ai_report")  # noqa
