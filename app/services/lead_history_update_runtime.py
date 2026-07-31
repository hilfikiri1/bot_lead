"""Bulk import of reviewed lead-history updates into Kommo.

The manager can paste a TSV/CSV table after ``/history_update`` or upload a
TXT/TSV/CSV/XLSX file with that caption.  The importer resolves every row to a
Kommo deal, builds a dry-run preview and performs only explicitly confirmed
writes:

* append one structured Kommo note;
* move the deal to an existing, non-protected pipeline stage;
* create one concrete Kommo task when text and date are present.

It never sends WhatsApp messages, deletes entities or closes/wins deals.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import re
import unicodedata
import zipfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from app.agent import actions, executor, planner, service as agent_service
from app.agent.contracts import AgentPlan, AgentReply
from app.config import get_settings
from app.services import identity_service, kommo_service, lead_status_sync_service
from app.services import phone_utils

settings = get_settings()
_INSTALLED = False
_MAX_ROWS = 200
_MAX_NOTE_LENGTH = 13_500
_ALLOWED_EXTENSIONS = {".txt", ".tsv", ".csv", ".xlsx"}
_COMMAND_RE = re.compile(
    r"(?is)^\s*(?:/history_update|/history_import|/update_history|"
    r"обнови(?:ть)?\s+истори(?:ю|и)\s+(?:общения|лидов|сделок)|"
    r"импорт\s+обновлени(?:й|я)\s+(?:kommo|коммо))\b"
)
_FILE_HINT_RE = re.compile(
    r"(?i)(?:/history_update|/history_import|/update_history|"
    r"обнови(?:ть)?\s+истори(?:ю|и)|lead[_ -]?history|kommo[_ -]?updates)"
)

_HEADER_ALIASES = {
    "chat": {
        "чат лид", "чат", "лид", "chat lead", "lead", "сделка клиент",
    },
    "number": {
        "№", "номер", "no", "nr", "lead number", "internal number",
        "внутренний номер",
    },
    "product": {
        "товар проект", "товар", "проект", "product project", "product",
    },
    "phone": {"телефон", "phone", "numer telefonu"},
    "email": {"email", "e mail", "почта"},
    "budget": {"бюджет", "budget"},
    "current_stage": {
        "текущая стадия kommo", "текущая стадия", "current kommo stage",
        "current stage",
    },
    "recommended_stage": {
        "рекомендуемая стадия", "recommended stage", "target stage",
        "новая стадия",
    },
    "priority": {"приоритет", "priority"},
    "action_group": {
        "группа действий", "action group", "рекомендуемое действие",
    },
    "comment": {
        "комментарий kommo", "комментарий", "примечание kommo",
        "kommo comment", "note",
    },
    "next_action": {
        "следующее действие", "next action", "следующая задача",
    },
    "next_date": {
        "дата следующего действия", "next action date", "дата задачи",
        "due date",
    },
    "followup": {
        "текст follow up", "текст followup", "follow up", "followup",
        "сообщение клиенту",
    },
    "kommo_id": {"id kommo", "kommo id", "id сделки", "lead id"},
}

_PROTECTED_STAGE_NAMES = {
    "закрыто",
    "успешно завершено",
    "успешно реализовано",
    "won",
    "closed",
    "delete",
    "удалить",
}

_STATUS_ALIASES = {
    "предложение отправлено ожидание решения": (
        "ожидание решения",
        "предложение отправлено",
    ),
    "предложение отправлено ожидание ответа": (
        "ожидание решения",
        "предложение отправлено",
    ),
    "nurture закрытие": ("nurture",),
    "проверка дубликата": (),
}


@dataclass
class HistoryUpdateRow:
    row_number: int
    chat: str = ""
    internal_number: str = ""
    product: str = ""
    phone: str = ""
    email: str = ""
    budget: str = ""
    current_stage: str = ""
    recommended_stage: str = ""
    priority: str = ""
    action_group: str = ""
    comment: str = ""
    next_action: str = ""
    next_date: str = ""
    followup: str = ""
    kommo_id: str = ""


def _clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\x00", "").split()).strip()


def _multiline_clean(value: Any) -> str:
    lines = [line.rstrip() for line in str(value or "").replace("\x00", "").splitlines()]
    return "\n".join(line for line in lines if line.strip()).strip()


def _normal(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().replace("ё", "е")
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _header_key(value: Any) -> str | None:
    raw = str(value or "").strip()
    if raw in {"№", "#"}:
        return "number"
    normalized = _normal(raw)
    for key, aliases in _HEADER_ALIASES.items():
        if normalized in {_normal(alias) for alias in aliases}:
            return key
    return None


def _header_score(values: Iterable[Any]) -> int:
    return len({key for key in (_header_key(value) for value in values) if key})


def _trim_trailing_empty(values: list[Any]) -> list[Any]:
    result = list(values)
    while result and not str(result[-1] or "").strip():
        result.pop()
    return result


def _records_from_matrix(matrix: list[list[Any]]) -> list[HistoryUpdateRow]:
    if not matrix:
        return []
    candidates = [
        (index, _header_score(row))
        for index, row in enumerate(matrix[:15])
    ]
    header_index, score = max(candidates, key=lambda item: item[1], default=(0, 0))
    if score < 4:
        raise ValueError(
            "Не найдена строка заголовков. Нужны как минимум: №/лид, товар, "
            "комментарий или следующее действие."
        )

    headers = _trim_trailing_empty([str(value or "").strip() for value in matrix[header_index]])
    width = len(headers)
    key_by_index = {index: _header_key(header) for index, header in enumerate(headers)}
    rows: list[HistoryUpdateRow] = []
    pending: dict[str, str] | None = None
    pending_row_number = header_index + 2

    def flush() -> None:
        nonlocal pending
        if not pending:
            return
        if any(
            _clean(pending.get(key))
            for key in ("chat", "internal_number", "product", "phone", "kommo_id", "comment")
        ):
            rows.append(
                HistoryUpdateRow(
                    row_number=pending_row_number,
                    **{field: str(pending.get(field) or "") for field in HistoryUpdateRow.__dataclass_fields__ if field != "row_number"},
                )
            )
        pending = None

    for physical_index, raw_row in enumerate(matrix[header_index + 1 :], start=header_index + 2):
        row = _trim_trailing_empty(list(raw_row))
        if not row or not any(str(value or "").strip() for value in row):
            continue

        # Continuation line from an unquoted multiline TSV cell.
        if len(row) == 1 and pending is not None:
            pending["comment"] = (
                pending.get("comment", "") + "\n" + str(row[0] or "")
            ).strip()
            continue

        # Common clipboard shape: final line of a multiline comment followed by
        # next-action and Excel serial date.
        if (
            pending is not None
            and len(row) in {2, 3}
            and _parse_date(row[-1]) is not None
        ):
            if len(row) == 3 and str(row[0] or "").strip():
                pending["comment"] = (
                    pending.get("comment", "") + "\n" + str(row[0] or "")
                ).strip()
            pending["next_action"] = str(row[-2] or "").strip()
            pending["next_date"] = str(row[-1] or "").strip()
            continue

        padded = list(row[:width]) + [""] * max(0, width - len(row))
        mapped: dict[str, str] = {}
        for index, value in enumerate(padded[:width]):
            key = key_by_index.get(index)
            if key:
                mapped[key] = str(value or "").strip()

        looks_like_record = bool(
            _clean(mapped.get("internal_number"))
            or _clean(mapped.get("kommo_id"))
            or _clean(mapped.get("chat"))
            or _clean(mapped.get("product"))
            or _clean(mapped.get("phone"))
        )
        if not looks_like_record and pending is not None:
            continuation = "\t".join(str(value or "") for value in row).strip()
            if continuation:
                pending["comment"] = (
                    pending.get("comment", "") + "\n" + continuation
                ).strip()
            continue

        flush()
        pending = {
            "chat": mapped.get("chat", ""),
            "internal_number": mapped.get("number", ""),
            "product": mapped.get("product", ""),
            "phone": mapped.get("phone", ""),
            "email": mapped.get("email", ""),
            "budget": mapped.get("budget", ""),
            "current_stage": mapped.get("current_stage", ""),
            "recommended_stage": mapped.get("recommended_stage", ""),
            "priority": mapped.get("priority", ""),
            "action_group": mapped.get("action_group", ""),
            "comment": mapped.get("comment", ""),
            "next_action": mapped.get("next_action", ""),
            "next_date": mapped.get("next_date", ""),
            "followup": mapped.get("followup", ""),
            "kommo_id": mapped.get("kommo_id", ""),
        }
        pending_row_number = physical_index

    flush()
    if len(rows) > _MAX_ROWS:
        raise ValueError(f"В одном импорте разрешено не более {_MAX_ROWS} строк.")
    if not rows:
        raise ValueError("В таблице не найдено ни одной строки с лидом.")
    return rows


def _parse_text_matrix(content: bytes) -> list[list[str]]:
    decoded = None
    for encoding in ("utf-8-sig", "utf-16", "cp1251"):
        try:
            decoded = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if decoded is None:
        raise ValueError("Не удалось определить кодировку текстового файла.")

    nonempty = next((line for line in decoded.splitlines() if line.strip()), "")
    delimiter = "\t"
    if nonempty.count("\t") < 2:
        delimiter = ";" if nonempty.count(";") >= nonempty.count(",") else ","
    return [list(row) for row in csv.reader(io.StringIO(decoded), delimiter=delimiter)]


def _xlsx_cell_value(cell: ET.Element, shared: list[str], ns: dict[str, str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//x:t", ns))
    value_node = cell.find("x:v", ns)
    raw = value_node.text if value_node is not None and value_node.text is not None else ""
    if cell_type == "s" and raw.isdigit():
        index = int(raw)
        return shared[index] if 0 <= index < len(shared) else ""
    if cell_type == "b":
        return "1" if raw == "1" else "0"
    return raw


def _column_index(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference or "")
    if not letters:
        return 0
    result = 0
    for char in letters.group(0):
        result = result * 26 + (ord(char) - 64)
    return result - 1


def _parse_xlsx_matrices(content: bytes) -> list[list[list[str]]]:
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rel_ns = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
    office_rel = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    matrices: list[list[list[str]]] = []

    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = [
                "".join(node.text or "" for node in item.findall(".//x:t", ns))
                for item in root.findall("x:si", ns)
            ]

        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        target_by_id = {
            rel.attrib.get("Id"): rel.attrib.get("Target")
            for rel in relationships.findall("r:Relationship", rel_ns)
        }
        for sheet in workbook.findall(".//x:sheets/x:sheet", ns):
            relation_id = sheet.attrib.get(office_rel)
            target = target_by_id.get(relation_id)
            if not target:
                continue
            path = target.lstrip("/")
            if not path.startswith("xl/"):
                path = f"xl/{path}"
            if path not in archive.namelist():
                continue
            root = ET.fromstring(archive.read(path))
            matrix: list[list[str]] = []
            for row_node in root.findall(".//x:sheetData/x:row", ns):
                values: dict[int, str] = {}
                for cell in row_node.findall("x:c", ns):
                    index = _column_index(cell.attrib.get("r", ""))
                    values[index] = _xlsx_cell_value(cell, shared, ns)
                if values:
                    width = max(values) + 1
                    matrix.append([values.get(index, "") for index in range(width)])
                else:
                    matrix.append([])
            matrices.append(matrix)
    return matrices


def parse_history_update_file(
    *, filename: str, content: bytes
) -> list[HistoryUpdateRow]:
    extension = Path(filename or "").suffix.casefold()
    if extension not in _ALLOWED_EXTENSIONS:
        raise ValueError(
            "Поддерживаются только TXT, TSV, CSV и XLSX для обновления истории."
        )
    if extension == ".xlsx":
        matrices = _parse_xlsx_matrices(content)
        scored = [
            (matrix, max((_header_score(row) for row in matrix[:15]), default=0))
            for matrix in matrices
        ]
        matrix, score = max(scored, key=lambda item: item[1], default=([], 0))
        if score < 4:
            raise ValueError("В XLSX не найден лист с таблицей обновлений лидов.")
        return _records_from_matrix(matrix)
    return _records_from_matrix(_parse_text_matrix(content))


def parse_history_update_text(text: str) -> list[HistoryUpdateRow]:
    payload = _COMMAND_RE.sub("", text or "", count=1).lstrip(" :—-\n")
    if not payload.strip():
        raise ValueError(
            "После команды вставьте TSV/CSV-таблицу или загрузите файл "
            "с подписью /history_update."
        )
    return _records_from_matrix(_parse_text_matrix(payload.encode("utf-8")))


def _parse_date(value: Any) -> date | None:
    raw = _clean(value)
    if not raw:
        return None
    try:
        serial = float(raw.replace(",", "."))
        if 20_000 <= serial <= 80_000:
            return date(1899, 12, 30) + timedelta(days=int(serial))
    except ValueError:
        pass
    for fmt in (
        "%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y",
    ):
        try:
            return datetime.strptime(raw[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _due_timestamp(value: Any) -> int | None:
    parsed = _parse_date(value)
    if parsed is None:
        return None
    try:
        timezone = ZoneInfo(settings.manager_timezone or "Europe/Warsaw")
    except Exception:
        timezone = ZoneInfo("Europe/Warsaw")
    return int(datetime.combine(parsed, time(hour=10), tzinfo=timezone).timestamp())


def _normalize_phone(value: Any) -> str:
    normalized = phone_utils.normalize_phone(str(value or ""))
    return normalized or re.sub(r"\D", "", str(value or ""))


def _normalize_email(value: Any) -> str:
    return str(value or "").strip().casefold()


def _status_candidates(value: str) -> tuple[str, ...]:
    normalized = _normal(value)
    if not normalized:
        return ()
    aliases = _STATUS_ALIASES.get(normalized)
    if aliases is not None:
        return aliases
    return (normalized,)


def _resolve_status(
    recommended: str,
    statuses: list[dict[str, Any]],
) -> tuple[int | None, str | None, str | None]:
    normalized = _normal(recommended)
    if not normalized:
        return None, None, None
    if normalized in _PROTECTED_STAGE_NAMES:
        return None, None, (
            f"Стадия «{recommended}» защищена: закрытие/успех не выполняются "
            "через массовое обновление."
        )
    by_name = {
        _normal(item.get("name")): item
        for item in statuses
        if isinstance(item.get("id"), int)
    }
    for candidate in _status_candidates(recommended):
        item = by_name.get(candidate)
        if item:
            return int(item["id"]), str(item.get("name") or recommended), None
    return None, None, f"Стадия «{recommended}» не найдена в воронке сделки."


def _lead_indexes(leads: list[dict[str, Any]]) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[int, dict[str, Any]],
    dict[str, list[dict[str, Any]]],
]:
    by_number: dict[str, list[dict[str, Any]]] = {}
    by_id: dict[int, dict[str, Any]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for lead in leads:
        lead_id = lead.get("id")
        if isinstance(lead_id, int):
            by_id[lead_id] = lead
        number = lead_status_sync_service.parse_internal_number(lead.get("name"))
        if number:
            by_number.setdefault(str(number), []).append(lead)
        normalized_name = _normal(lead.get("name"))
        if normalized_name:
            by_name.setdefault(normalized_name, []).append(lead)
    return by_number, by_id, by_name


async def _resolve_rows(
    rows: list[HistoryUpdateRow],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    result = await kommo_service.get_all_leads_for_status_sync()
    leads = list(result.get("leads") or [])
    by_number, by_id, by_name = _lead_indexes(leads)
    enriched: list[dict[str, Any]] | None = None
    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []

    async def contact_indexes() -> tuple[
        dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]
    ]:
        nonlocal enriched
        if enriched is None:
            enriched = await kommo_service.enrich_leads_with_contacts(leads)
        by_phone: dict[str, list[dict[str, Any]]] = {}
        by_email: dict[str, list[dict[str, Any]]] = {}
        for lead in enriched:
            for phone in lead.get("phones") or []:
                normalized = _normalize_phone(phone)
                if normalized:
                    by_phone.setdefault(normalized, []).append(lead)
            for email in lead.get("emails") or []:
                normalized_email = _normalize_email(email)
                if normalized_email:
                    by_email.setdefault(normalized_email, []).append(lead)
        return by_phone, by_email

    for row in rows:
        candidates: list[dict[str, Any]] = []
        matched_by = ""

        if _clean(row.kommo_id).isdigit():
            lead = by_id.get(int(_clean(row.kommo_id)))
            candidates = [lead] if lead else []
            matched_by = "Kommo ID"
        elif _clean(row.internal_number).isdigit():
            candidates = list(by_number.get(str(int(_clean(row.internal_number))), []))
            matched_by = "внутренний номер"
        else:
            by_phone, by_email = await contact_indexes()
            phone = _normalize_phone(row.phone)
            email = _normalize_email(row.email)
            if phone:
                candidates = list(by_phone.get(phone, []))
                matched_by = "телефон"
            if not candidates and email:
                candidates = list(by_email.get(email, []))
                matched_by = "email"
            if not candidates:
                normalized_chat = _normal(row.chat)
                exact = by_name.get(normalized_chat, [])
                if exact:
                    candidates = list(exact)
                    matched_by = "точное название"
                else:
                    possible = [
                        lead
                        for lead in leads
                        if normalized_chat
                        and (
                            normalized_chat in _normal(lead.get("name"))
                            or _normal(lead.get("name")) in normalized_chat
                        )
                    ]
                    if len(possible) == 1:
                        candidates = possible
                        matched_by = "название"

        if len(candidates) == 1 and candidates[0]:
            resolved.append(
                {
                    "row": row,
                    "lead": candidates[0],
                    "matched_by": matched_by,
                }
            )
        elif len(candidates) > 1:
            ambiguous.append(
                {
                    "row_number": row.row_number,
                    "lead_number": row.internal_number,
                    "chat": row.chat,
                    "candidate_ids": [item.get("id") for item in candidates],
                }
            )
        else:
            unresolved.append(
                {
                    "row_number": row.row_number,
                    "lead_number": row.internal_number,
                    "chat": row.chat,
                    "phone": row.phone,
                }
            )
    return resolved, unresolved, ambiguous


def _history_note(
    row: HistoryUpdateRow,
    *,
    digest: str,
    lead_id: int,
    matched_by: str,
) -> tuple[str, str]:
    marker = f"[BBS-HISTORY-UPDATE:{digest}:{row.row_number}:{lead_id}]"
    lines = [
        marker,
        "ОБНОВЛЕНИЕ ИСТОРИИ ОБЩЕНИЯ С ЛИДОМ",
        "",
        f"Источник сопоставления: {matched_by or 'не указан'}",
    ]
    if row.product:
        lines.append(f"Товар/проект: {row.product}")
    if row.priority:
        lines.append(f"Приоритет: {row.priority}")
    if row.action_group:
        lines.append(f"Группа действий: {row.action_group}")
    if row.current_stage:
        lines.append(f"Стадия до анализа: {row.current_stage}")
    if row.recommended_stage:
        lines.append(f"Рекомендуемая стадия: {row.recommended_stage}")
    if row.comment:
        lines.extend(["", "КОММЕНТАРИЙ", _multiline_clean(row.comment)])
    if row.next_action:
        lines.extend(["", "СЛЕДУЮЩЕЕ ДЕЙСТВИЕ", _multiline_clean(row.next_action)])
        if row.next_date:
            parsed = _parse_date(row.next_date)
            lines.append(
                f"Дата: {parsed.strftime('%d.%m.%Y') if parsed else row.next_date}"
            )
    if row.followup:
        lines.extend(
            [
                "",
                "ЧЕРНОВИК FOLLOW-UP — НЕ ОТПРАВЛЕН",
                _multiline_clean(row.followup),
            ]
        )
    if _normal(row.action_group) == "удалить":
        lines.extend(
            [
                "",
                "ВАЖНО: удаление не выполнено. Массовый импорт не удаляет карточки.",
            ]
        )
    return marker, "\n".join(lines)[:_MAX_NOTE_LENGTH]


async def build_history_update_preview(
    rows: list[HistoryUpdateRow],
    *,
    source_name: str,
) -> dict[str, Any]:
    actor = identity_service.current_user()
    if actor is not None and actor.role not in {"owner", "admin"}:
        raise PermissionError(
            "Массовое обновление истории доступно только Owner и Admin."
        )

    resolved, unresolved, ambiguous = await _resolve_rows(rows)
    status_cache: dict[int, list[dict[str, Any]]] = {}
    stable_rows = [asdict(item["row"]) for item in resolved]
    digest = hashlib.sha256(
        json.dumps(stable_rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    items: list[dict[str, Any]] = []
    warnings: list[str] = []

    for resolved_item in resolved:
        row: HistoryUpdateRow = resolved_item["row"]
        lead = resolved_item["lead"]
        lead_id = int(lead["id"])
        pipeline_id = int(lead.get("pipeline_id") or 0)
        statuses = status_cache.get(pipeline_id)
        if statuses is None:
            statuses = (
                await kommo_service.get_pipeline_statuses(pipeline_id)
                if pipeline_id
                else []
            )
            status_cache[pipeline_id] = statuses
        status_id, status_name, stage_warning = _resolve_status(
            row.recommended_stage, statuses
        )
        marker, note_text = _history_note(
            row,
            digest=digest,
            lead_id=lead_id,
            matched_by=str(resolved_item.get("matched_by") or ""),
        )
        due_timestamp = _due_timestamp(row.next_date)
        item_warnings: list[str] = []
        if stage_warning:
            item_warnings.append(stage_warning)
        if row.next_action and not due_timestamp:
            item_warnings.append(
                "Задача не будет создана: дата отсутствует или не распознана."
            )
        if lead.get("closed_at"):
            item_warnings.append(
                "Сделка уже закрыта: задача и изменение стадии будут пропущены."
            )
        if _normal(row.action_group) == "удалить":
            item_warnings.append("Удаление карточки запрещено и будет пропущено.")
        warnings.extend(
            f"Строка {row.row_number}: {warning}" for warning in item_warnings
        )
        items.append(
            {
                "lead_id": lead_id,
                "lead_name": lead.get("name"),
                "lead_url": lead.get("url"),
                "pipeline_id": pipeline_id,
                "current_status_id": lead.get("status_id"),
                "current_status_name": lead.get("status_name"),
                "closed_at": lead.get("closed_at"),
                "internal_lead_number": row.internal_number,
                "source_row": row.row_number,
                "marker": marker,
                "note_text": note_text,
                "target_status_id": status_id,
                "target_status_name": status_name,
                "task_text": _multiline_clean(row.next_action)[:1000],
                "task_due_at": due_timestamp,
                "followup_text": _multiline_clean(row.followup),
                "warnings": item_warnings,
            }
        )

    return {
        "source_name": source_name,
        "digest": digest,
        "rows_count": len(rows),
        "resolved_count": len(items),
        "unresolved": unresolved,
        "ambiguous": ambiguous,
        "warnings": warnings,
        "items": items,
        "item_results": {},
    }


def format_history_update_preview(report: dict[str, Any]) -> str:
    items = list(report.get("items") or [])
    lines = [
        "<b>🧾 Обновление истории лидов — предпросмотр</b>",
        "",
        f"Источник: <code>{html.escape(str(report.get('source_name') or 'вставленный текст'))}</code>",
        f"Строк: <b>{int(report.get('rows_count') or 0)}</b>",
        f"Надёжно найдено в Kommo: <b>{len(items)}</b>",
        f"Не найдено: <b>{len(report.get('unresolved') or [])}</b>",
        f"Неоднозначно: <b>{len(report.get('ambiguous') or [])}</b>",
        "",
        "<b>Будет выполнено после подтверждения</b>",
        "• добавление структурированного примечания;",
        "• изменение стадии, только если она найдена в текущей воронке;",
        "• создание задачи, только если есть текст и дата.",
        "",
        "<b>Не выполняется</b>: отправка WhatsApp, удаление, закрытие и отметка успеха.",
    ]
    for item in items[:12]:
        stage = (
            f"{item.get('current_status_name') or '—'} → {item.get('target_status_name')}"
            if item.get("target_status_id")
            else str(item.get("current_status_name") or "без изменения стадии")
        )
        task = " · задача" if item.get("task_text") and item.get("task_due_at") else ""
        lines.append(
            f"• <b>{html.escape(str(item.get('lead_name') or item.get('lead_id')))}</b>"
            f" — {html.escape(stage)}{task}"
        )
    if len(items) > 12:
        lines.append(f"…и ещё {len(items) - 12}")
    warnings = list(report.get("warnings") or [])
    if warnings:
        lines.extend(["", f"⚠️ Предупреждений: <b>{len(warnings)}</b>"])
        lines.extend(f"• {html.escape(str(value))}" for value in warnings[:5])
    unresolved = list(report.get("unresolved") or [])
    ambiguous = list(report.get("ambiguous") or [])
    if unresolved or ambiguous:
        lines.extend(
            [
                "",
                "Ненадёжно сопоставленные строки не попадут в пакет. "
                "Их нужно исправить и импортировать повторно.",
            ]
        )
    return "\n".join(lines)[:4000]


async def _stage_report(
    db: Any,
    *,
    report: dict[str, Any],
    chat_id: int,
    telegram_user_id: int,
) -> AgentReply:
    if not report.get("items"):
        return AgentReply(
            format_history_update_preview(report)
            + "\n\n❌ Нет ни одной строки, которую можно безопасно обновить.",
            intent="lead_history_update_empty",
            metadata=report,
        )
    preview = format_history_update_preview(report)
    action = await actions.stage_action(
        db,
        telegram_user_id=telegram_user_id,
        chat_id=chat_id,
        action_type="apply_lead_history_updates_batch",
        payload=report,
        preview_text=preview,
    )
    markup = actions.approval_markup(action.id)
    markup["inline_keyboard"][0][0]["text"] = (
        f"✅ Обновить {len(report.get('items') or [])} лидов"
    )
    return AgentReply(
        preview,
        reply_markup=markup,
        intent="lead_history_update",
        metadata={"action_id": int(action.id), **report},
    )


def _task_already_exists(tasks: list[dict[str, Any]], text_value: str) -> bool:
    wanted = _normal(text_value)
    return any(_normal(item.get("text")) == wanted for item in tasks if wanted)


async def _execute_history_update_batch(action: Any) -> dict[str, Any]:
    payload = dict(action.payload or {})
    items = list(payload.get("items") or [])
    item_results = dict(
        payload.get("item_results")
        or (action.result or {}).get("item_results")
        or {}
    )
    lines = ["<b>Результат обновления истории лидов</b>", ""]
    success = 0
    failed = 0

    for item in items:
        lead_id = int(item["lead_id"])
        key = str(lead_id)
        prior = item_results.get(key) or {}
        if prior.get("status") == "ok":
            success += 1
            lines.append(
                f"✅ {html.escape(str(item.get('lead_name') or lead_id))} — уже выполнено"
            )
            continue
        try:
            details = await kommo_service.get_lead_details(lead_id)
            notes = await kommo_service.get_recent_common_notes(lead_id, limit=50)
            marker = str(item.get("marker") or "")
            note_created = False
            if marker and not any(marker in str(note.get("text") or "") for note in notes):
                await kommo_service.add_common_note(
                    lead_id, str(item.get("note_text") or "")
                )
                note_created = True

            stage_updated = False
            target_status_id = item.get("target_status_id")
            if (
                target_status_id
                and not details.get("closed_at")
                and int(details.get("status_id") or 0) != int(target_status_id)
            ):
                await kommo_service.update_kommo_lead(
                    lead_id, status_id=int(target_status_id)
                )
                stage_updated = True

            task_created = False
            task_text = str(item.get("task_text") or "").strip()
            task_due_at = item.get("task_due_at")
            if task_text and task_due_at and not details.get("closed_at"):
                tasks = await kommo_service.get_open_lead_tasks(lead_id, limit=50)
                if not _task_already_exists(tasks, task_text):
                    await kommo_service.create_lead_task(
                        lead_id=lead_id,
                        text=task_text[:1000],
                        complete_till=int(task_due_at),
                    )
                    task_created = True

            result = {
                "status": "ok",
                "note_created": note_created,
                "stage_updated": stage_updated,
                "task_created": task_created,
            }
            item_results[key] = result
            success += 1
            actions_done = [
                label
                for done, label in (
                    (note_created, "примечание"),
                    (stage_updated, "стадия"),
                    (task_created, "задача"),
                )
                if done
            ]
            lines.append(
                f"✅ {html.escape(str(item.get('lead_name') or lead_id))} — "
                + (", ".join(actions_done) if actions_done else "без повторных изменений")
            )
        except Exception as exc:
            failed += 1
            item_results[key] = {
                "status": "failed",
                "error": str(exc)[:500],
            }
            lines.append(
                f"❌ {html.escape(str(item.get('lead_name') or lead_id))} — "
                f"{html.escape(str(exc)[:180])}"
            )

    payload["item_results"] = item_results
    action.payload = payload
    lines.extend(["", f"Выполнено: <b>{success}</b> · Ошибок: <b>{failed}</b>"])
    return {
        "text": "\n".join(lines)[:4000],
        "data": {"item_results": item_results, "items": items},
        "partial_failed": failed > 0,
        "error_message": "Часть лидов не обновлена." if failed else None,
    }


def _is_history_file(filename: str, caption: str | None) -> bool:
    extension = Path(filename or "").suffix.casefold()
    if extension not in _ALLOWED_EXTENSIONS:
        return False
    return bool(
        _FILE_HINT_RE.search(caption or "")
        or _FILE_HINT_RE.search(Path(filename or "").stem)
    )


def install_lead_history_update_runtime() -> None:
    """Install the command/file importer without changing the core agent flow."""

    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_deterministic_plan = planner.deterministic_plan

    def deterministic_plan_with_history_update(
        text: str, context: dict[str, Any]
    ) -> AgentPlan | None:
        if _COMMAND_RE.search(text or ""):
            payload = _COMMAND_RE.sub("", text or "", count=1).lstrip(" :—-\n")
            return AgentPlan(
                intent="lead_history_update",
                mode="write" if payload.strip() else "clarify",
                confidence=1.0,
                body=payload or None,
                clarification_question=(
                    None
                    if payload.strip()
                    else (
                        "Вставьте TSV/CSV-таблицу после /history_update или "
                        "загрузите TXT/CSV/XLSX с этой подписью."
                    )
                ),
            )
        return original_deterministic_plan(text, context)

    planner.deterministic_plan = deterministic_plan_with_history_update

    original_execute_plan = agent_service._execute_plan

    async def execute_plan_with_history_update(
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
        if plan.intent != "lead_history_update":
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
                "🔒 Массовое обновление истории доступно только Owner и Admin.",
                intent="permission_denied",
            )
        try:
            rows = parse_history_update_text(text)
            report = await build_history_update_preview(
                rows, source_name="вставленный текст"
            )
            return await _stage_report(
                db,
                report=report,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
            )
        except Exception as exc:
            return AgentReply(
                "❌ <b>Не удалось разобрать обновление истории</b>\n\n"
                f"<code>{html.escape(str(exc)[:900])}</code>",
                intent="lead_history_update_failed",
            )

    agent_service._execute_plan = execute_plan_with_history_update

    original_file_upload = agent_service.handle_project_file_upload

    async def handle_file_upload_with_history_update(
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
        if not _is_history_file(filename, caption):
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
                "🔒 Массовое обновление истории доступно только Owner и Admin.",
                intent="permission_denied",
            )
        try:
            rows = parse_history_update_file(filename=filename, content=content)
            report = await build_history_update_preview(
                rows, source_name=filename
            )
            return await _stage_report(
                db,
                report=report,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
            )
        except Exception as exc:
            return AgentReply(
                "❌ <b>Не удалось импортировать файл обновлений</b>\n\n"
                f"<code>{html.escape(str(exc)[:900])}</code>",
                intent="lead_history_update_failed",
            )

    agent_service.handle_project_file_upload = handle_file_upload_with_history_update

    original_execute = executor._execute

    async def execute_with_history_update(
        db: Any, action: Any
    ) -> dict[str, Any]:
        if action.action_type == "apply_lead_history_updates_batch":
            return await _execute_history_update_batch(action)
        return await original_execute(db, action)

    executor._execute = execute_with_history_update
