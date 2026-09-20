"""Bulk reviewed JSON commands for Kommo CRM.

This runtime is intentionally deterministic: ChatGPT or another analyst may prepare
JSON, but the bot resolves every lead and stage against live Kommo data, shows one
preview, and writes only after the existing Telegram approval button is pressed.

Supported actions:
- keep
- update
- move
- add_note
- close_lost
- close_won
- delete (recognized but intentionally not executed because the public Kommo v4
  lead API does not expose a supported delete operation)
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path
from typing import Any

from app.agent import actions, executor, planner, service as agent_service
from app.agent.contracts import AgentPlan, AgentReply
from app.agent.lead_refs import extract_internal_lead_number
from app.services import identity_service, kommo_service

_INSTALLED = False
_MAX_ACTIONS = 200
_ALLOWED_ACTIONS = {
    "keep",
    "update",
    "move",
    "add_note",
    "close_lost",
    "close_won",
    "delete",
}
_ALLOWED_CHANGE_KEYS = {"name", "price", "stage_name"}
_COMMAND_RE = re.compile(r"(?is)^\s*/bulk_json\b")
_CODE_FENCE_RE = re.compile(r"(?is)^\s*```(?:json)?\s*(.*?)\s*```\s*$")
_JSON_FILE_EXTENSIONS = {".json"}

_WON_STAGE_NAMES = {
    "won",
    "closed won",
    "success",
    "successful",
    "успешно",
    "успешно реализовано",
    "успешно завершено",
    "закрыто успешно",
    "wygrane",
    "zakończono sukcesem",
    "zamknięte wygrane",
}
_LOST_STAGE_NAMES = {
    "lost",
    "closed lost",
    "unsuccessful",
    "закрыто и не реализовано",
    "закрыто неуспешно",
    "не реализовано",
    "неуспешно",
    "проиграно",
    "przegrane",
    "zamknięte przegrane",
    "zakończono bez sukcesu",
}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _normal(value: Any) -> str:
    return " ".join(
        re.sub(r"[^\w]+", " ", str(value or "").casefold().replace("ё", "е")).split()
    )


def _extract_json_body(text: str) -> str:
    body = _COMMAND_RE.sub("", text or "", count=1).lstrip(" :—-\n\t")
    fence = _CODE_FENCE_RE.match(body)
    if fence:
        body = fence.group(1)
    return body.strip()


def _parse_price(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("price должен быть числом.")
    if isinstance(value, (int, float)):
        return max(0, int(round(float(value))))
    raw = str(value or "").strip().replace(" ", "").replace(",", ".")
    if not raw:
        raise ValueError("price не может быть пустым.")
    try:
        return max(0, int(round(float(raw))))
    except ValueError as exc:
        raise ValueError(f"Некорректный price: {value}") from exc


def _normalize_ref(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"actions[{index}].lead_ref должен быть объектом.")

    kommo_id = raw.get("kommo_id")
    internal = raw.get("internal_number")
    if kommo_id not in (None, "") and internal not in (None, ""):
        raise ValueError(
            f"actions[{index}].lead_ref: укажите либо kommo_id, либо internal_number."
        )
    if kommo_id not in (None, ""):
        try:
            value = int(kommo_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"actions[{index}].lead_ref.kommo_id некорректен.") from exc
        if value <= 0:
            raise ValueError(f"actions[{index}].lead_ref.kommo_id должен быть > 0.")
        return {"kommo_id": value}

    if internal not in (None, ""):
        digits = re.sub(r"\D", "", str(internal))
        if not digits or int(digits) <= 0 or int(digits) > 9999:
            raise ValueError(
                f"actions[{index}].lead_ref.internal_number должен быть 1..9999."
            )
        return {"internal_number": str(int(digits))}

    raise ValueError(
        f"actions[{index}].lead_ref: нужен kommo_id или internal_number."
    )


def _normalize_changes(raw: Any, index: int) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"actions[{index}].changes должен быть объектом.")
    unknown = set(raw) - _ALLOWED_CHANGE_KEYS
    if unknown:
        raise ValueError(
            f"actions[{index}].changes: неизвестные поля: {', '.join(sorted(unknown))}."
        )

    result: dict[str, Any] = {}
    if "name" in raw:
        name = _clean(raw.get("name"))
        if not name:
            raise ValueError(f"actions[{index}].changes.name не может быть пустым.")
        result["name"] = name[:255]
    if "price" in raw:
        result["price"] = _parse_price(raw.get("price"))
    if "stage_name" in raw:
        stage_name = _clean(raw.get("stage_name"))
        if not stage_name:
            raise ValueError(
                f"actions[{index}].changes.stage_name не может быть пустым."
            )
        result["stage_name"] = stage_name[:255]
    return result


def parse_bulk_json_text(text: str) -> dict[str, Any]:
    body = _extract_json_body(text)
    if not body:
        raise ValueError(
            "После /bulk_json вставьте JSON или загрузите .json файл с подписью /bulk_json."
        )
    try:
        raw = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Невалидный JSON: строка {exc.lineno}, колонка {exc.colno}."
        ) from exc
    return validate_bulk_payload(raw)


def parse_bulk_json_file(content: bytes) -> dict[str, Any]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("JSON-файл должен быть в UTF-8.") from exc
    fence = _CODE_FENCE_RE.match(text)
    if fence:
        text = fence.group(1)
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Невалидный JSON: строка {exc.lineno}, колонка {exc.colno}."
        ) from exc
    return validate_bulk_payload(raw)


def validate_bulk_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Корневой JSON должен быть объектом.")
    version = raw.get("version", 1)
    if version != 1:
        raise ValueError("Поддерживается только version=1.")

    raw_actions = raw.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise ValueError("Поле actions должно быть непустым массивом.")
    if len(raw_actions) > _MAX_ACTIONS:
        raise ValueError(f"В одном пакете разрешено не более {_MAX_ACTIONS} лидов.")

    normalized: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    for index, item in enumerate(raw_actions):
        if not isinstance(item, dict):
            raise ValueError(f"actions[{index}] должен быть объектом.")

        action = _clean(item.get("action")).casefold()
        if action not in _ALLOWED_ACTIONS:
            raise ValueError(
                f"actions[{index}].action={action!r} не поддерживается."
            )
        lead_ref = _normalize_ref(item.get("lead_ref"), index)
        ref_key = json.dumps(lead_ref, sort_keys=True, ensure_ascii=False)
        if ref_key in seen_refs:
            raise ValueError(
                f"actions[{index}]: один и тот же lead_ref указан больше одного раза."
            )
        seen_refs.add(ref_key)

        changes = _normalize_changes(item.get("changes"), index)
        note = str(item.get("note") or "").strip()
        if len(note) > 13_500:
            raise ValueError(f"actions[{index}].note длиннее 13 500 символов.")

        if action == "move" and not changes.get("stage_name"):
            raise ValueError(
                f"actions[{index}]: move требует changes.stage_name."
            )
        if action == "add_note" and not note:
            raise ValueError(f"actions[{index}]: add_note требует note.")
        if action == "update" and not changes and not note:
            raise ValueError(
                f"actions[{index}]: update требует changes и/или note."
            )
        if action == "keep" and (changes or note):
            raise ValueError(
                f"actions[{index}]: keep не должен содержать changes или note."
            )

        normalized.append(
            {
                "lead_ref": lead_ref,
                "action": action,
                "changes": changes,
                "note": note,
            }
        )

    return {"version": 1, "actions": normalized}


def _match_stage(
    requested: str,
    statuses: list[dict[str, Any]],
) -> tuple[int | None, str | None]:
    wanted = _normal(requested)
    exact = [
        item
        for item in statuses
        if _normal(item.get("name")) == wanted and isinstance(item.get("id"), int)
    ]
    if len(exact) == 1:
        return int(exact[0]["id"]), str(exact[0].get("name") or requested)

    fuzzy = [
        item
        for item in statuses
        if isinstance(item.get("id"), int)
        and wanted
        and (
            wanted in _normal(item.get("name"))
            or _normal(item.get("name")) in wanted
        )
    ]
    if len(fuzzy) == 1:
        return int(fuzzy[0]["id"]), str(fuzzy[0].get("name") or requested)
    return None, None


def _match_terminal_stage(
    action: str,
    statuses: list[dict[str, Any]],
) -> tuple[int | None, str | None]:
    candidates = _WON_STAGE_NAMES if action == "close_won" else _LOST_STAGE_NAMES
    normalized_candidates = {_normal(value) for value in candidates}

    exact = [
        item
        for item in statuses
        if isinstance(item.get("id"), int)
        and _normal(item.get("name")) in normalized_candidates
    ]
    if len(exact) == 1:
        return int(exact[0]["id"]), str(exact[0].get("name") or "")

    keywords = (
        ("успеш", "won", "success", "wygran", "sukces")
        if action == "close_won"
        else ("не реализ", "неусп", "lost", "przegran", "bez sukces")
    )
    fuzzy = [
        item
        for item in statuses
        if isinstance(item.get("id"), int)
        and any(token in _normal(item.get("name")) for token in keywords)
    ]
    if len(fuzzy) == 1:
        return int(fuzzy[0]["id"]), str(fuzzy[0].get("name") or "")
    return None, None


def _ref_label(ref: dict[str, Any]) -> str:
    if ref.get("internal_number"):
        return f"№{ref['internal_number']}"
    return f"Kommo ID {ref.get('kommo_id')}"


def _item_label(item: dict[str, Any]) -> str:
    lead_id = int(item.get("lead_id") or 0)
    internal = item.get("internal_lead_number")
    lead_name = _clean(item.get("lead_name")) or str(lead_id)
    if internal:
        return f"№{internal} · {lead_name} · Kommo ID {lead_id}"
    return f"Kommo ID {lead_id} · {lead_name}"


async def build_bulk_preview(payload: dict[str, Any]) -> dict[str, Any]:
    actor = identity_service.current_user()
    if actor is not None and actor.role not in {"owner", "admin"}:
        raise PermissionError(
            "Массовые изменения Kommo доступны только Owner и Admin."
        )

    open_result = await kommo_service.get_all_open_leads(
        allow_menu_fallback=False
    )
    open_leads = list(open_result.get("leads") or [])
    by_id = {
        int(item["id"]): item
        for item in open_leads
        if isinstance(item.get("id"), int)
    }
    by_internal: dict[str, list[dict[str, Any]]] = {}
    for lead in open_leads:
        internal = extract_internal_lead_number(lead)
        if internal:
            by_internal.setdefault(str(internal), []).append(lead)

    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    status_cache: dict[int, list[dict[str, Any]]] = {}
    used_lead_ids: set[int] = set()
    items: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    warnings: list[str] = []

    for source_index, command in enumerate(payload["actions"], start=1):
        ref = dict(command["lead_ref"])
        lead: dict[str, Any] | None = None

        if ref.get("kommo_id"):
            lead_id = int(ref["kommo_id"])
            lead = by_id.get(lead_id)
            if lead is None:
                try:
                    lead = await kommo_service.get_lead_details(lead_id)
                except Exception as exc:
                    unresolved.append(
                        {
                            "index": source_index,
                            "ref": ref,
                            "reason": str(exc)[:300],
                        }
                    )
                    continue
        else:
            internal = str(ref.get("internal_number") or "")
            matches = list(by_internal.get(internal) or [])
            if len(matches) == 1:
                lead = matches[0]
            elif len(matches) > 1:
                unresolved.append(
                    {
                        "index": source_index,
                        "ref": ref,
                        "reason": "Найдено несколько открытых сделок с этим внутренним номером.",
                        "candidate_ids": [item.get("id") for item in matches],
                    }
                )
                continue
            else:
                unresolved.append(
                    {
                        "index": source_index,
                        "ref": ref,
                        "reason": "Открытая сделка с этим внутренним номером не найдена.",
                    }
                )
                continue

        lead_id = int(lead.get("id") or 0)
        if not lead_id:
            unresolved.append(
                {
                    "index": source_index,
                    "ref": ref,
                    "reason": "У сделки нет корректного Kommo ID.",
                }
            )
            continue
        if lead_id in used_lead_ids:
            unresolved.append(
                {
                    "index": source_index,
                    "ref": ref,
                    "reason": f"Эта сделка уже есть в пакете как Kommo ID {lead_id}.",
                }
            )
            continue
        used_lead_ids.add(lead_id)

        action = str(command["action"])
        changes = dict(command.get("changes") or {})
        note = str(command.get("note") or "").strip()
        pipeline_id = int(lead.get("pipeline_id") or 0)
        update_payload: dict[str, Any] = {"id": lead_id}
        item_warnings: list[str] = []
        executable = action not in {"keep", "delete"}

        if action == "delete":
            executable = False
            warning = (
                "Удаление распознано, но не будет выполнено: публичный Kommo v4 "
                "Lead API не предоставляет поддерживаемый delete endpoint."
            )
            item_warnings.append(warning)
            warnings.append(f"{_ref_label(ref)}: {warning}")

        if action in {"update", "move"}:
            if "name" in changes:
                update_payload["name"] = str(changes["name"])
            if "price" in changes:
                update_payload["price"] = int(changes["price"])

        requested_stage = changes.get("stage_name")
        target_status_name = None
        if action in {"update", "move"} and requested_stage:
            if pipeline_id not in status_cache:
                status_cache[pipeline_id] = (
                    await kommo_service.get_pipeline_statuses(pipeline_id)
                    if pipeline_id
                    else []
                )
            status_id, target_status_name = _match_stage(
                str(requested_stage), status_cache[pipeline_id]
            )
            if status_id is None:
                executable = False
                reason = (
                    f"Этап «{requested_stage}» не найден однозначно "
                    "в текущей воронке."
                )
                item_warnings.append(reason)
                unresolved.append(
                    {
                        "index": source_index,
                        "ref": ref,
                        "reason": reason,
                    }
                )
            else:
                update_payload["status_id"] = int(status_id)

        if action in {"close_lost", "close_won"}:
            if pipeline_id not in status_cache:
                status_cache[pipeline_id] = (
                    await kommo_service.get_pipeline_statuses(pipeline_id)
                    if pipeline_id
                    else []
                )
            status_id, target_status_name = _match_terminal_stage(
                action, status_cache[pipeline_id]
            )
            if status_id is None:
                executable = False
                reason = (
                    "Не удалось однозначно определить финальный этап "
                    + ("успешной" if action == "close_won" else "неуспешной")
                    + " сделки в текущей воронке."
                )
                item_warnings.append(reason)
                unresolved.append(
                    {
                        "index": source_index,
                        "ref": ref,
                        "reason": reason,
                    }
                )
            else:
                update_payload["status_id"] = int(status_id)

        has_update = len(update_payload) > 1
        if action == "add_note":
            has_update = False
        if action == "keep":
            has_update = False

        internal = extract_internal_lead_number(lead) or ref.get("internal_number")
        items.append(
            {
                "source_index": source_index,
                "lead_id": lead_id,
                "internal_lead_number": internal,
                "lead_name": lead.get("name") or str(lead_id),
                "lead_url": lead.get("url"),
                "pipeline_id": pipeline_id,
                "current_status_id": lead.get("status_id"),
                "current_status_name": lead.get("status_name"),
                "action": action,
                "changes": changes,
                "update_payload": update_payload if has_update else None,
                "target_status_name": target_status_name,
                "note_text": note,
                "executable": bool(executable and (has_update or note)),
                "warnings": item_warnings,
                "digest": digest,
            }
        )

    return {
        "version": 1,
        "digest": digest,
        "actions_count": len(payload["actions"]),
        "items": items,
        "unresolved": unresolved,
        "warnings": warnings,
        "item_results": {},
    }


def format_bulk_preview(report: dict[str, Any]) -> str:
    items = list(report.get("items") or [])
    executable = [item for item in items if item.get("executable")]
    keeps = [item for item in items if item.get("action") == "keep"]
    deletes = [item for item in items if item.get("action") == "delete"]
    unresolved = list(report.get("unresolved") or [])

    counts: dict[str, int] = {}
    for item in items:
        action = str(item.get("action") or "unknown")
        counts[action] = counts.get(action, 0) + 1

    lines = [
        "<b>🧾 Kommo Bulk JSON — предпросмотр</b>",
        "",
        f"Команд в JSON: <b>{int(report.get('actions_count') or 0)}</b>",
        f"Надёжно сопоставлено: <b>{len(items)}</b>",
        f"Будет изменено после подтверждения: <b>{len(executable)}</b>",
        f"Оставить без изменений: <b>{len(keeps)}</b>",
        f"Delete (не выполняется): <b>{len(deletes)}</b>",
        f"Не найдено / неоднозначно: <b>{len(unresolved)}</b>",
        "",
        "<b>Действия</b>",
    ]
    labels = {
        "keep": "оставить",
        "update": "обновить",
        "move": "перенести",
        "add_note": "добавить заметку",
        "close_lost": "закрыть как неуспешный",
        "close_won": "закрыть как успешный",
        "delete": "delete — пропустить",
    }
    for item in items[:15]:
        label = _item_label(item)
        detail = labels.get(str(item.get("action")), str(item.get("action")))
        if item.get("target_status_name"):
            detail += f" → {item['target_status_name']}"
        if (item.get("changes") or {}).get("price") is not None:
            detail += f" · сумма {item['changes']['price']}"
        if item.get("note_text"):
            detail += " · заметка"
        lines.append(
            f"• <b>{html.escape(label)}</b> — {html.escape(detail)}"
        )
    if len(items) > 15:
        lines.append(f"…и ещё {len(items) - 15}")

    if unresolved:
        lines.extend(["", "<b>⚠️ Не войдут в пакет</b>"])
        for item in unresolved[:8]:
            lines.append(
                f"• {html.escape(_ref_label(item.get('ref') or {}))} — "
                f"{html.escape(str(item.get('reason') or 'не найдено'))}"
            )
        if len(unresolved) > 8:
            lines.append(f"…и ещё {len(unresolved) - 8}")

    if deletes:
        lines.extend(
            [
                "",
                "🗑 <b>Важно:</b> команды delete только отображаются в отчёте и "
                "не заменяются автоматически на закрытие сделки.",
            ]
        )
    if executable:
        lines.extend(
            [
                "",
                "Никаких изменений ещё не сделано. Нажмите кнопку подтверждения.",
            ]
        )
    else:
        lines.extend(["", "В пакете нет поддерживаемых изменений для выполнения."])
    return "\n".join(lines)[:4000]


async def _stage_report(
    db: Any,
    *,
    report: dict[str, Any],
    chat_id: int,
    telegram_user_id: int,
) -> AgentReply:
    preview = format_bulk_preview(report)
    executable_count = sum(
        1 for item in report.get("items") or [] if item.get("executable")
    )
    if not executable_count:
        return AgentReply(
            preview,
            intent="kommo_bulk_json_preview",
            metadata=report,
        )

    action = await actions.stage_action(
        db,
        telegram_user_id=telegram_user_id,
        chat_id=chat_id,
        action_type="apply_kommo_bulk_json_batch",
        payload=report,
        preview_text=preview,
    )
    markup = actions.approval_markup(action.id)
    markup["inline_keyboard"][0][0]["text"] = (
        f"✅ Выполнить {executable_count} изменений"
    )
    return AgentReply(
        preview,
        reply_markup=markup,
        intent="kommo_bulk_json",
        metadata={"action_id": int(action.id), **report},
    )


def _fallback_update_kwargs(update_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: update_payload[key]
        for key in ("name", "price", "status_id", "pipeline_id")
        if key in update_payload
    }


async def _execute_bulk_json(action: Any) -> dict[str, Any]:
    payload = dict(action.payload or {})
    items = list(payload.get("items") or [])
    item_results = dict(
        payload.get("item_results")
        or (action.result or {}).get("item_results")
        or {}
    )

    pending_updates: list[dict[str, Any]] = []
    update_items: list[dict[str, Any]] = []
    pending_notes: list[dict[str, Any]] = []
    note_items: list[dict[str, Any]] = []

    for item in items:
        lead_id = int(item["lead_id"])
        key = str(lead_id)
        prior = dict(item_results.get(key) or {})
        operations = dict(prior.get("operations") or {})

        if item.get("action") == "keep":
            item_results[key] = {
                "status": "ok",
                "operations": operations,
                "message": "оставлено без изменений",
            }
            continue
        if item.get("action") == "delete":
            item_results[key] = {
                "status": "skipped",
                "operations": operations,
                "message": "delete не поддерживается публичным Kommo Lead API",
            }
            continue
        if not item.get("executable"):
            item_results[key] = {
                "status": "skipped",
                "operations": operations,
                "message": "команда не прошла проверку preview",
            }
            continue
        if prior.get("status") == "ok":
            continue

        update_payload = item.get("update_payload")
        if isinstance(update_payload, dict) and len(update_payload) > 1:
            if (operations.get("update") or {}).get("status") != "ok":
                pending_updates.append(dict(update_payload))
                update_items.append(item)
        note_text = str(item.get("note_text") or "").strip()
        if note_text and (operations.get("note") or {}).get("status") != "ok":
            pending_notes.append({"lead_id": lead_id, "text": note_text})
            note_items.append(item)

        item_results[key] = {
            **prior,
            "operations": operations,
        }

    if pending_updates:
        try:
            await kommo_service.update_kommo_leads_bulk(pending_updates)
            for item in update_items:
                key = str(int(item["lead_id"]))
                operations = dict(item_results.get(key, {}).get("operations") or {})
                operations["update"] = {"status": "ok"}
                item_results[key] = {
                    **item_results.get(key, {}),
                    "operations": operations,
                }
        except Exception:
            for item in update_items:
                key = str(int(item["lead_id"]))
                operations = dict(item_results.get(key, {}).get("operations") or {})
                try:
                    await kommo_service.update_kommo_lead(
                        int(item["lead_id"]),
                        **_fallback_update_kwargs(dict(item["update_payload"])),
                    )
                    operations["update"] = {"status": "ok"}
                except Exception as exc:
                    operations["update"] = {
                        "status": "failed",
                        "error": str(exc)[:500],
                    }
                item_results[key] = {
                    **item_results.get(key, {}),
                    "operations": operations,
                }

    if pending_notes:
        try:
            await kommo_service.add_common_notes_bulk(pending_notes)
            for item in note_items:
                key = str(int(item["lead_id"]))
                operations = dict(item_results.get(key, {}).get("operations") or {})
                operations["note"] = {"status": "ok"}
                item_results[key] = {
                    **item_results.get(key, {}),
                    "operations": operations,
                }
        except Exception:
            for item in note_items:
                key = str(int(item["lead_id"]))
                operations = dict(item_results.get(key, {}).get("operations") or {})
                try:
                    await kommo_service.add_common_note(
                        int(item["lead_id"]), str(item.get("note_text") or "")
                    )
                    operations["note"] = {"status": "ok"}
                except Exception as exc:
                    operations["note"] = {
                        "status": "failed",
                        "error": str(exc)[:500],
                    }
                item_results[key] = {
                    **item_results.get(key, {}),
                    "operations": operations,
                }

    success = 0
    failed = 0
    skipped = 0
    lines = ["<b>Результат Kommo Bulk JSON</b>", ""]

    for item in items:
        lead_id = int(item["lead_id"])
        key = str(lead_id)
        result = dict(item_results.get(key) or {})
        operations = dict(result.get("operations") or {})
        if result.get("status") == "skipped":
            skipped += 1
        elif result.get("status") == "ok" and item.get("action") == "keep":
            success += 1
        else:
            required: list[str] = []
            update_payload = item.get("update_payload")
            if isinstance(update_payload, dict) and len(update_payload) > 1:
                required.append("update")
            if str(item.get("note_text") or "").strip():
                required.append("note")

            failures = [
                op
                for op in required
                if (operations.get(op) or {}).get("status") != "ok"
            ]
            if failures:
                failed += 1
                result["status"] = "failed"
            else:
                success += 1
                result["status"] = "ok"
            result["operations"] = operations
            item_results[key] = result

        label = _item_label(item)
        final_status = item_results.get(key, {}).get("status")
        icon = "✅" if final_status == "ok" else ("⏭" if final_status == "skipped" else "❌")
        lines.append(
            f"{icon} {html.escape(label)} — "
            f"{html.escape(str(item.get('action') or ''))}"
        )

    payload["item_results"] = item_results
    action.payload = payload
    lines.extend(
        [
            "",
            f"Успешно: <b>{success}</b> · Ошибок: <b>{failed}</b> · "
            f"Пропущено: <b>{skipped}</b>",
        ]
    )
    return {
        "text": "\n".join(lines)[:4000],
        "data": {"item_results": item_results, "items": items},
        "partial_failed": failed > 0,
        "error_message": "Часть лидов не обновлена." if failed else None,
    }


def _is_bulk_json_file(filename: str, caption: str | None) -> bool:
    return (
        Path(filename or "").suffix.casefold() in _JSON_FILE_EXTENSIONS
        and bool(_COMMAND_RE.search(caption or ""))
    )


def install_kommo_bulk_json_runtime() -> None:
    """Install /bulk_json command, JSON upload and executor extension."""

    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_deterministic_plan = planner.deterministic_plan

    def deterministic_plan_with_bulk_json(
        text: str, context: dict[str, Any]
    ) -> AgentPlan | None:
        if _COMMAND_RE.search(text or ""):
            body = _extract_json_body(text)
            return AgentPlan(
                intent="kommo_bulk_json",
                mode="write" if body else "clarify",
                confidence=1.0,
                body=body or None,
                clarification_question=(
                    None
                    if body
                    else (
                        "Вставьте JSON после /bulk_json или загрузите .json файл "
                        "с подписью /bulk_json."
                    )
                ),
            )
        return original_deterministic_plan(text, context)

    planner.deterministic_plan = deterministic_plan_with_bulk_json

    original_execute_plan = agent_service._execute_plan

    async def execute_plan_with_bulk_json(
        db: Any,
        *,
        plan: AgentPlan,
        text: str,
        chat_id: int,
        telegram_user_id: int,
        source: str,
        context: dict[str, Any],
        session: Any,
        pre_resolved_leads: list[dict[str, Any]] | None = None,
    ) -> AgentReply:
        if plan.intent != "kommo_bulk_json":
            return await original_execute_plan(
                db,
                plan=plan,
                text=text,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                source=source,
                context=context,
                session=session,
                pre_resolved_leads=pre_resolved_leads,
            )
        actor = identity_service.current_user()
        if actor is not None and actor.role not in {"owner", "admin"}:
            return AgentReply(
                "🔒 Массовые изменения Kommo доступны только Owner и Admin.",
                intent="permission_denied",
            )
        try:
            parsed = parse_bulk_json_text(text)
            report = await build_bulk_preview(parsed)
            return await _stage_report(
                db,
                report=report,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
            )
        except Exception as exc:
            return AgentReply(
                "❌ <b>Не удалось разобрать Bulk JSON</b>\n\n"
                f"<code>{html.escape(str(exc)[:900])}</code>",
                intent="kommo_bulk_json_failed",
            )

    agent_service._execute_plan = execute_plan_with_bulk_json

    original_file_upload = agent_service.handle_project_file_upload

    async def handle_file_upload_with_bulk_json(
        db: Any,
        *,
        chat_id: int,
        telegram_user_id: int,
        telegram_message_id: int | None = None,
        filename: str,
        mime_type: str,
        content: bytes,
        caption: str | None = None,
        kind: str | None = None,
    ) -> AgentReply:
        if not _is_bulk_json_file(filename, caption):
            return await original_file_upload(
                db,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                telegram_message_id=telegram_message_id,
                filename=filename,
                mime_type=mime_type,
                content=content,
                caption=caption,
                kind=kind,
            )
        actor = identity_service.current_user()
        if actor is not None and actor.role not in {"owner", "admin"}:
            return AgentReply(
                "🔒 Массовые изменения Kommo доступны только Owner и Admin.",
                intent="permission_denied",
            )
        try:
            parsed = parse_bulk_json_file(content)
            report = await build_bulk_preview(parsed)
            return await _stage_report(
                db,
                report=report,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
            )
        except Exception as exc:
            return AgentReply(
                "❌ <b>Не удалось импортировать Bulk JSON</b>\n\n"
                f"<code>{html.escape(str(exc)[:900])}</code>",
                intent="kommo_bulk_json_failed",
            )

    agent_service.handle_project_file_upload = handle_file_upload_with_bulk_json

    original_execute = executor._execute

    async def execute_with_bulk_json(db: Any, action: Any) -> dict[str, Any]:
        if action.action_type == "apply_kommo_bulk_json_batch":
            return await _execute_bulk_json(action)
        return await original_execute(db, action)

    executor._execute = execute_with_bulk_json
