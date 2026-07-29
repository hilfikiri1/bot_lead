from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_voice_pipeline_stages_notion_write_when_agent_enabled():
    source = (ROOT / "app/tasks/voice_note_tasks.py").read_text(encoding="utf-8")
    assert 'action_type="sync_call_analysis_to_notion"' in source
    assert "notion_action_id=notion_action_id" in source
    assert "if settings.agent_enabled:" in source


def test_report_has_separate_notion_confirmation_button():
    source = (ROOT / "app/services/telegram_service.py").read_text(encoding="utf-8")
    assert '"📓 Сохранить анализ в Notion"' in source
    assert 'f"agent:ok:{notion_action_id}"' in source


def test_migration_continues_from_current_head():
    source = (ROOT / "migrations/versions/007_unified_agent_v3.py").read_text(
        encoding="utf-8"
    )
    assert 'revision = "007_unified_agent_v3"' in source
    assert 'down_revision = "007_operational_agent_v2"' in source
    assert 'op.create_table(\n        "integration_events"' not in source


def test_voice_command_preserves_selected_kommo_lead_context():
    source = (ROOT / "app/tasks/voice_note_tasks.py").read_text(encoding="utf-8")
    assert "active_kommo_lead_id=target_kommo_lead_id" in source


def test_calendar_diagnostics_are_read_only_from_menu_and_command():
    source = (ROOT / "app/api/telegram.py").read_text(encoding="utf-8")
    assert "menu:calendar" in source
    assert source.count("include_write_probe=False") >= 2
    assert "Тестовая запись в календарь отключена" in source
