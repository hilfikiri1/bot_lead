"""Strict contracts for CRM-aware telephone conversation analysis."""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MatchMethod = Literal[
    "lead_id",
    "kommo_deal_id",
    "phone",
    "email",
    "name_company",
    "unresolved",
]
PriorityValue = Literal["A1", "A2", "B", "C", "D"]
WaitingFor = Literal["client", "manager", "factory", "logistics", "other"]
StageName = Literal[
    "Первый контакт",
    "Сбор информации",
    "Квалификация лида",
    # Backward-compatible values accepted from older prompts. The call policy
    # normalizes them to the real Kommo stages before any write.
    "Квалификация",
    "Ожидание данных клиента",
    "Получено ТЗ",
    "Поиск поставщиков",
    "Получены предложения фабрик",
    "Подготовка расчёта",
    "Предложение отправлено",
    "Ожидание решения",
    "Образцы",
    "PI/договор",
    "Ожидание оплаты",
]

_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_LETTER_RE = re.compile(r"[A-Za-zА-Яа-яЁё]")


def _require_russian_narrative(value: str, *, field_name: str) -> str:
    """Reject long internal prose with no Cyrillic characters.

    Names, brands, email addresses and short codes may legitimately be Latin.
    Narrative CRM fields must be Russian so a Polish transcript cannot leak into
    the internal Kommo note, manager task or Telegram report. Validation failure
    triggers the existing single JSON repair pass.
    """
    clean = str(value or "").strip()
    letters = _LETTER_RE.findall(clean)
    if len(letters) >= 12 and not _CYRILLIC_RE.search(clean):
        raise ValueError(f"{field_name} must be written in Russian")
    return clean


def _require_russian_list(values: list[str], *, field_name: str) -> list[str]:
    return [
        _require_russian_narrative(str(value), field_name=field_name)
        for value in values
    ]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CallIdentity(StrictModel):
    lead_id: str = ""
    contact_name: str = ""
    company_name: str = ""
    phone: str = ""
    email: str = ""
    identity_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    match_method: MatchMethod = "unresolved"


class PriorityDecision(StrictModel):
    value: PriorityValue
    reason: str

    @field_validator("reason")
    @classmethod
    def reason_must_be_russian(cls, value: str) -> str:
        return _require_russian_narrative(value, field_name="priority.reason")


class KommoUpdateDecision(StrictModel):
    should_add_note: bool = True
    note: str = ""
    should_change_stage: bool = False
    new_stage: StageName | None = None
    stage_reason: str = ""
    should_create_task: bool = True
    task_title: str = ""
    task_description: str = ""
    task_due_date: date | None = None

    @field_validator("note", "stage_reason", "task_title", "task_description")
    @classmethod
    def internal_update_text_must_be_russian(
        cls, value: str, info: Any
    ) -> str:
        return _require_russian_narrative(value, field_name=f"kommo_update.{info.field_name}")

    @model_validator(mode="after")
    def validate_conditional_fields(self) -> "KommoUpdateDecision":
        if self.should_change_stage and not self.new_stage:
            raise ValueError("new_stage is required when should_change_stage is true")
        if self.should_create_task:
            if not self.task_title:
                raise ValueError("task_title is required when should_create_task is true")
            if not self.task_description:
                raise ValueError(
                    "task_description is required when should_create_task is true"
                )
        return self


class ClientMessageDraft(StrictModel):
    language: str = "pl"
    channel: Literal["WhatsApp", "email"] = "WhatsApp"
    text: str = ""
    send_automatically: Literal[False] = False

    @field_validator("text")
    @classmethod
    def reject_template_placeholders(cls, value: str) -> str:
        forbidden = ("[Imię", "[Name", "[Имя", "{{", "}}")
        if any(token in value for token in forbidden):
            raise ValueError("client message contains an unresolved template placeholder")
        return value


class ActionCompleted(StrictModel):
    action: Literal[
        "note_created",
        "stage_updated",
        "task_created",
        "task_updated",
    ]
    status: Literal["success", "failed", "skipped"]
    old_value: str | None = None
    new_value: str | None = None
    due_date: date | None = None
    error: str | None = None


class CRMCallAnalysis(StrictModel):
    identity: CallIdentity
    summary: str
    known_from_crm: list[str] = Field(default_factory=list)
    confirmed_in_call: list[str] = Field(default_factory=list)
    new_information: list[str] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    client_goal: str = ""
    client_commitment: str = ""
    manager_commitment: str = ""
    waiting_for: WaitingFor = "other"
    priority: PriorityDecision
    kommo_update: KommoUpdateDecision
    client_message: ClientMessageDraft
    needs_review: bool = False
    review_reason: str = ""
    actions_completed: list[ActionCompleted] = Field(default_factory=list)

    @field_validator(
        "summary",
        "client_goal",
        "client_commitment",
        "manager_commitment",
        "review_reason",
    )
    @classmethod
    def internal_narrative_must_be_russian(cls, value: str, info: Any) -> str:
        return _require_russian_narrative(value, field_name=info.field_name)

    @field_validator(
        "known_from_crm",
        "confirmed_in_call",
        "new_information",
        "inferences",
        "unknown",
        "contradictions",
    )
    @classmethod
    def internal_lists_must_be_russian(
        cls, values: list[str], info: Any
    ) -> list[str]:
        return _require_russian_list(values, field_name=info.field_name)

    @model_validator(mode="after")
    def review_requires_reason(self) -> "CRMCallAnalysis":
        if self.needs_review and not self.review_reason:
            raise ValueError("review_reason is required when needs_review is true")
        return self


class LeadContext(StrictModel):
    lead_id: str = ""
    kommo_deal_id: int | None = None
    lead_name: str = ""
    contact_id: str = ""
    contact_name: str = ""
    company_name: str = ""
    phone: str = ""
    email: str = ""
    region: str = ""
    product_from_form: str = ""
    budget_from_form: str = ""
    preferred_channel: str = ""
    current_stage: str = ""
    current_priority: str = ""
    current_task: str = ""
    previous_notes: list[str] = Field(default_factory=list)
    custom_fields: dict[str, str] = Field(default_factory=dict)
    pipeline_id: int | None = None
    status_id: int | None = None
    responsible_user_id: int | None = None
    kommo_url: str = ""


class CallContext(StrictModel):
    call_date: date
    call_time: str
    manager_name: str
    transcript: str


class CRMCallInput(StrictModel):
    lead_context: LeadContext
    call_context: CallContext
