from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


AgentMode = Literal["answer", "read", "draft", "write", "conversation", "clarify"]


class AgentPlan(BaseModel):
    """Structured plan produced by deterministic rules or the language model."""

    intent: str = "general_assistant"
    mode: AgentMode = "answer"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    lead_id: int | None = None
    query: str | None = None
    lead_refs: list[dict[str, Any]] = Field(default_factory=list)
    resolved_lead_ids: list[int] = Field(default_factory=list)
    draft_kind: str | None = None
    title: str | None = None
    body: str | None = None
    note_text: str | None = None
    due_at: str | None = None
    duration_minutes: int = 30
    reminder_minutes: int = 30
    event_type: str = "call"
    fields: dict[str, Any] = Field(default_factory=dict)
    language: str = "auto"
    clarification_question: str | None = None
    rationale: str | None = None

    @field_validator("duration_minutes")
    @classmethod
    def valid_duration(cls, value: int) -> int:
        return max(5, min(int(value or 30), 24 * 60))

    @field_validator("reminder_minutes")
    @classmethod
    def valid_reminder(cls, value: int) -> int:
        return max(0, min(int(value or 0), 30 * 24 * 60))


@dataclass
class AgentReply:
    text: str
    reply_markup: dict[str, Any] | None = None
    handled: bool = True
    intent: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    ok: bool
    text: str
    data: dict[str, Any] = field(default_factory=dict)
    reply_markup: dict[str, Any] | None = None
