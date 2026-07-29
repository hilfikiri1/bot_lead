from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.database import Base


class ProjectMemory(Base):
    __tablename__ = "project_memories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kommo_lead_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    project_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    memory_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    requirements: Mapped[str | None] = mapped_column(Text, nullable=True)
    decisions: Mapped[str | None] = mapped_column(Text, nullable=True)
    promises: Mapped[str | None] = mapped_column(Text, nullable=True)
    missing_information: Mapped[str | None] = mapped_column(Text, nullable=True)
    risks: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class LeadAssessment(Base):
    __tablename__ = "lead_assessments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kommo_lead_id: Mapped[int] = mapped_column(BigInteger, index=True)
    grade: Mapped[str] = mapped_column(String(8), index=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    reasons_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    risks_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    missing_data_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    recommended_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="rules")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class NextActionState(Base):
    __tablename__ = "next_action_states"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kommo_lead_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="missing")
    waiting_on: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    action_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    responsible_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stale_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IntegrationOperation(Base):
    __tablename__ = "integration_operations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    operation_type: Mapped[str] = mapped_column(String(64), index=True)
    service: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="pending")
    kommo_lead_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserNotificationPreference(Base):
    __tablename__ = "user_notification_preferences"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    morning_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evening_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    digest_enabled: Mapped[bool] = mapped_column(default=True)
    plan_enabled: Mapped[bool] = mapped_column(default=True)
    settings_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class SheetsLeadLink(Base):
    __tablename__ = "sheets_lead_links"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kommo_lead_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    sheet_row: Mapped[int | None] = mapped_column(Integer, nullable=True)
    internal_lead_number: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    phone_normalized: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class DocumentExtraction(Base):
    __tablename__ = "document_extractions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_artifact_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    kommo_lead_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    document_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extracted_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    conflicts_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UndoOperation(Base):
    __tablename__ = "undo_operations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    original_action_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kommo_lead_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    undo_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    reversible: Mapped[bool] = mapped_column(default=True)
    before_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
