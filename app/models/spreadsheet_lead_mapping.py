from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SpreadsheetLeadMapping(Base):
    __tablename__ = "spreadsheet_lead_mappings"

    id: Mapped[int] = mapped_column(primary_key=True)
    kommo_lead_id: Mapped[int] = mapped_column(BigInteger, index=True)
    spreadsheet_lead_number: Mapped[str] = mapped_column(String(32))
    original_product: Mapped[str | None] = mapped_column(Text, nullable=True)
    short_product_ru: Mapped[str | None] = mapped_column(String(120), nullable=True)
    old_kommo_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_kommo_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    spreadsheet_row_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matched_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    matched_value_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by_telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
