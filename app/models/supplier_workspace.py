from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ProjectSupplier(Base):
    """A supplier/factory candidate linked to one Kommo project."""

    __tablename__ = "project_suppliers"
    __table_args__ = (
        UniqueConstraint(
            "kommo_lead_id",
            "identity_key",
            name="uq_project_suppliers_lead_identity",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kommo_lead_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    internal_lead_number: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    identity_key: Mapped[str] = mapped_column(String(64), nullable=False)
    platform: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_channel: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contact_value: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="candidate", server_default="candidate", index=True
    )
    verification_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_checked", server_default="not_checked", index=True
    )
    verification_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_followup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by_telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SupplierInquiry(Base):
    """Outgoing price/specification request and response tracking."""

    __tablename__ = "supplier_inquiries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    supplier_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("project_suppliers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kommo_lead_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="prepared", server_default="prepared", index=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reply_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    response_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by_telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SupplierOffer(Base):
    """Normalized commercial offer used for side-by-side comparison."""

    __tablename__ = "supplier_offers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    supplier_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("project_suppliers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kommo_lead_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="received", server_default="received", index=True
    )
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD", server_default="USD")
    incoterm: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    named_place: Mapped[str | None] = mapped_column(String(255), nullable=True)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    total_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    moq: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lead_time_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    warranty_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_terms: Mapped[str | None] = mapped_column(String(500), nullable=True)
    packaging: Mapped[str | None] = mapped_column(Text, nullable=True)
    certifications_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    key_components_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    deviations_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    source_artifact_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("project_artifacts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
