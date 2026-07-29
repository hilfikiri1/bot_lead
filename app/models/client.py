from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    kommo_contact_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, unique=True, index=True
    )
    notion_page_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(50))
    email: Mapped[str | None] = mapped_column(String(255))
    company: Mapped[str | None] = mapped_column(String(255))
    language: Mapped[str | None] = mapped_column(String(10))
    communication_language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    communication_language_source: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    communication_language_confidence: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    communication_language_set_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    communication_language_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    leads: Mapped[list["Lead"]] = relationship("Lead", back_populates="client")  # noqa
