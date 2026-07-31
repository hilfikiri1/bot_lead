"""Strict contracts for CRM-aware telephone conversation analysis."""

from __future__ import annotations

from datetime import date
from typing import Literal

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
