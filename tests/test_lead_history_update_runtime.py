from __future__ import annotations

from datetime import date

from app.services import lead_history_update_runtime as history


def test_parse_excel_serial_date():
    assert history._parse_date("46234") == date(2026, 7, 31)
    assert history._parse_date("46237") == date(2026, 8, 3)


def test_parse_pasted_tsv_with_multiline_comment():
    text = (
        "/history_update\n"
        "Чат / лид\t№\tТовар / проект\tТелефон\tТекущая стадия Kommo\t"
        "Рекомендуемая стадия\tПриоритет\tГруппа действий\t"
        "Комментарий Kommo\tСледующее действие\tДата следующего действия\n"
        "100 Andrzej\t100\tСкважинные трубы\t+48 500 000 000\t"
        "Получен ответ\tОжидание данных клиента\tB\tFollow-up\t"
        "31.07.2026 — анализ WhatsApp\n"
        "Клиент обещал прислать фотографии.\n"
        "Следующее действие: напомнить\tНаписать короткое напоминание\t46237\n"
    )

    rows = history.parse_history_update_text(text)

    assert len(rows) == 1
    row = rows[0]
    assert row.internal_number == "100"
    assert row.product == "Скважинные трубы"
    assert "Клиент обещал" in row.comment
    assert row.next_action == "Написать короткое напоминание"
    assert row.next_date == "46237"


def test_protected_stage_is_never_resolved():
    statuses = [
        {"id": 10, "name": "Первый контакт"},
        {"id": 99, "name": "Закрыто"},
    ]

    status_id, status_name, warning = history._resolve_status("Закрыто", statuses)

    assert status_id is None
    assert status_name is None
    assert "защищена" in warning


def test_combined_stage_prefers_waiting_for_decision():
    statuses = [
        {"id": 10, "name": "Предложение отправлено"},
        {"id": 20, "name": "Ожидание решения"},
    ]

    status_id, status_name, warning = history._resolve_status(
        "Предложение отправлено / ожидание решения",
        statuses,
    )

    assert status_id == 20
    assert status_name == "Ожидание решения"
    assert warning is None


def test_file_requires_explicit_history_hint():
    assert history._is_history_file("kommo_updates.xlsx", None)
    assert history._is_history_file("report.xlsx", "/history_update")
    assert not history._is_history_file(
        "supplier_offer.xlsx", "предложение фабрики для проекта 135"
    )
