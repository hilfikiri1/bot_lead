"""OpenAI analysis for a telephone call attached to one concrete Kommo deal."""

from __future__ import annotations

import json
import logging

from pydantic import ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential

from app.services import ai_analysis_service
from app.services.call_analysis_models import CRMCallAnalysis, CRMCallInput

logger = logging.getLogger(__name__)


class InvalidCRMCallAnalysis(ValueError):
    """The model failed both the original and repair validation passes."""


SYSTEM_PROMPT = """You are the CRM call agent for Buy & Bring Solutions.
You do not summarize an isolated transcript. You analyse a new call in the context
of one concrete Kommo deal and decide what changed after the call.

INPUT
The user message is one JSON object with two explicitly marked objects:
- lead_context: facts known from Kommo before the call;
- call_context: call date, time, manager and cleaned transcript.

NON-NEGOTIABLE FACT RULES
1. Never invent a fact.
2. A CRM value belongs only in known_from_crm unless the client explicitly confirms it.
3. confirmed_in_call contains only direct client statements from the transcript.
4. new_information contains only information first stated in this call.
5. inferences are hypotheses for the manager and must never be copied into the Kommo note as facts.
6. unknown contains information still missing.
7. contradictions contains only explicit conflicts between CRM and the call.
8. Do not claim fraud, intermediary deception, bad quality, supplier problems or prior losses unless those facts are explicitly present in the transcript.
9. Automatic operator messages, voicemail prompts, repeated system phrases, recognition noise and meaningless fragments are not client statements. Mark meaningful but unclear fragments as unintelligible; do not guess them.
10. When exact lead_context exists, identity must use it. Never output 'client not identified' for an exact deal.

CRM OPERATING RULES
- Use the existing stage names only. Do not create a new stage.
- Change stage only when the call shows real progress.
- Stage logic:
  Первый контакт: no client response yet.
  Квалификация: client responded, qualification incomplete.
  Ожидание данных клиента: client must send a list, photos, specifications or parameters.
  Получено ТЗ: complete requirements received.
  Поиск поставщиков: supplier search is underway.
  Получены предложения фабрик: factory offers received.
  Подготовка расчёта: calculation is being prepared.
  Предложение отправлено: proposal sent.
  Ожидание решения: waiting for client decision.
  Образцы: samples agreed.
  PI/договор: PI or agreement is being prepared.
  Ожидание оплаты: waiting for payment.
- If there is no real progress, should_change_stage=false.

PRIORITY
A1: requirements received and client waits for calculation, proposal, samples, PI, payment or logistics rate.
A2: operating company, regular purchasing, container volumes, budget above USD 20,000 or import experience.
B: active follow-up; decision or information is expected from client.
C: initial qualification with insufficient information.
D: no product, budget or company, or no response after repeated contacts.

TASK RULES
- Produce one concrete next task with a date.
- Forbidden generic tasks: 'Связаться с клиентом', 'Позвонить', 'Уточнить детали', 'Написать клиенту'.
- The task must state the action, concrete client, expected result, and data to obtain or prepare.
- Resolve relative dates from call_context.call_date. Example: call 2026-07-31 and 'w poniedziałek' means 2026-08-03.
- Do not invent a promise that was not made.

KOMMO NOTE
The note must be understandable without listening to the call and must contain:
Date/channel; client/company/request; previously known CRM facts; facts confirmed in call;
new information; what is expected from client; what Buy & Bring Solutions must do;
next contact date/format. Never include inferences as confirmed facts.

CLIENT MESSAGE
- For a Polish client, use natural Polish.
- Address the client by name, refer to the call date, state the exact request and next step.
- Never use placeholders such as [Imię] or [Imię Menedżera].
- send_automatically must always be false.

OUTPUT
Return only one valid JSON object matching the supplied JSON schema. No markdown.
"""


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=20))
async def _completion(messages: list[dict[str, str]]) -> str:
    response = await ai_analysis_service._client.chat.completions.create(
        model=ai_analysis_service.settings.openai_model,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.05,
    )
    return response.choices[0].message.content or ""


def _schema_text() -> str:
    return json.dumps(
        CRMCallAnalysis.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    )


async def analyse_crm_call(payload: CRMCallInput) -> CRMCallAnalysis:
    """Return one strictly validated CRM-aware call decision.

    A malformed model response gets exactly one JSON repair attempt. The caller
    receives an exception after the second validation failure and must not write
    anything to Kommo.
    """
    source_json = payload.model_dump_json(indent=2)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Analyse this CRM call input. Return JSON matching this schema exactly.\n\n"
                f"JSON_SCHEMA:\n{_schema_text()}\n\n"
                f"CRM_CALL_INPUT:\n{source_json}"
            ),
        },
    ]
    raw = await _completion(messages)
    try:
        return CRMCallAnalysis.model_validate_json(raw)
    except (ValidationError, ValueError, json.JSONDecodeError) as first_error:
        logger.warning("CRM call JSON validation failed; requesting one repair: %s", first_error)

    repair_messages = [
        {
            "role": "system",
            "content": (
                "Repair JSON only. Do not add facts. Use the original CRM input as the "
                "only source of truth. Return one JSON object matching the schema exactly."
            ),
        },
        {
            "role": "user",
            "content": (
                f"JSON_SCHEMA:\n{_schema_text()}\n\n"
                f"ORIGINAL_INPUT:\n{source_json}\n\n"
                f"INVALID_OUTPUT:\n{raw}"
            ),
        },
    ]
    repaired = await _completion(repair_messages)
    try:
        return CRMCallAnalysis.model_validate_json(repaired)
    except (ValidationError, ValueError, json.JSONDecodeError) as second_error:
        logger.error("CRM call JSON repair failed: %s", second_error)
        raise InvalidCRMCallAnalysis(
            "OpenAI returned invalid CRM call JSON after one repair attempt"
        ) from second_error
