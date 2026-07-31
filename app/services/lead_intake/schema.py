"""Strictly validated OpenAI response schema for lead qualification.

We ask OpenAI for Structured Outputs (JSON Schema, strict mode) when the
configured model supports it, and always re-validate the result with these
Pydantic models regardless of which response mode the API actually used.
Invalid content is never written to Kommo (see ``ai_service.py``).
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Potential = Literal["high", "medium", "low", "unknown"]
Readiness = Literal["high", "medium", "low", "unknown"]
Priority = Literal["A", "B", "C", "D", "SPAM"]
RecommendedAction = Literal["whatsapp", "phone_call", "email", "manual_review"]
TaskType = Literal["follow_up", "call", "email_follow_up", "manual_review"]
DueRule = Literal["today", "next_business_day", "in_2_business_days", "manual"]


class ClientMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str = Field(min_length=2, max_length=8)
    channel: RecommendedAction
    text: str = Field(min_length=1, max_length=4000)


class CallScript(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str
    # Manager-facing Russian briefing used in Telegram "Prepare call".
    company_context_ru: str = ""
    personal_analysis_ru: str = ""
    priority_note_ru: str = ""
    # The single fork question that removes the main uncertainty first.
    main_question_pl: str = ""
    main_question_reason_ru: str = ""
    # Longer Polish conversation scenario the manager can follow almost verbatim.
    conversation_script_pl: str = ""
    opening_phrase: str
    questions: list[str] = Field(default_factory=list)
    clarify_points_ru: list[str] = Field(default_factory=list)
    cheat_sheet_ru: list[str] = Field(default_factory=list)
    possible_objections: list[str] = Field(default_factory=list)
    recommended_answers: list[str] = Field(default_factory=list)
    must_record_after_call: list[str] = Field(default_factory=list)
    closing_phrase: str


class TaskPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: TaskType
    title_ru: str = Field(min_length=1, max_length=500)
    due_rule: DueRule
    due_at: Optional[str] = None


class SecondFollowUp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    days_after_first: int = Field(ge=0, le=30)
    text: Optional[str] = None


class LeadQualification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_name_ru: str = Field(min_length=1, max_length=80)
    potential: Potential
    readiness: Readiness
    priority: Priority
    priority_label_ru: str
    recommended_action: RecommendedAction
    recommended_action_reason_ru: str
    lead_analysis_ru: str
    main_risks_ru: list[str] = Field(default_factory=list)
    missing_information_ru: list[str] = Field(default_factory=list)
    next_steps_ru: list[str] = Field(default_factory=list)
    client_message: ClientMessage
    call_script: Optional[CallScript] = None
    kommo_note_ru: str
    task: TaskPlan
    second_follow_up: SecondFollowUp


class LeadQualificationError(RuntimeError):
    """Raised when the AI response cannot be turned into valid content."""


def _to_strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a Pydantic JSON schema for OpenAI Structured Outputs strict mode.

    Strict mode requires every object schema to set ``additionalProperties:
    false`` and list every property (including optional ones) in
    ``required``. Pydantic already omits ``additionalProperties`` and only
    lists non-default fields as required, so we recursively patch both.
    """

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                node = dict(node)
                properties = node.get("properties")
                if isinstance(properties, dict):
                    node["properties"] = {
                        key: _walk(value) for key, value in properties.items()
                    }
                    node["required"] = list(properties.keys())
                node["additionalProperties"] = False
            for key, value in list(node.items()):
                if key in {"properties", "additionalProperties", "required"}:
                    continue
                node[key] = _walk(value)
            return node
        if isinstance(node, list):
            return [_walk(item) for item in node]
        return node

    return _walk(schema)


def build_response_schema() -> dict[str, Any]:
    """Return an OpenAI-compatible strict JSON schema for ``LeadQualification``."""
    raw = LeadQualification.model_json_schema()
    return _to_strict_json_schema(raw)
