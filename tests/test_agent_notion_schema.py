from app.agent import notion_gateway


def test_project_schema_uses_russian_operational_properties():
    assert notion_gateway.PROJECT_SCHEMA["Название"] == "title"
    assert notion_gateway.PROJECT_SCHEMA["Kommo ID"] == "number"
    assert notion_gateway.PROJECT_SCHEMA["Родительский клиент"] == "relation"


def test_task_schema_contains_dedup_and_sync_fields():
    assert notion_gateway.TASK_SCHEMA["External ID"] == "rich_text"
    assert notion_gateway.TASK_SCHEMA["Sync status"] == "select"
    assert notion_gateway.TASK_SCHEMA["Обновить Kommo"] == "checkbox"


def test_agent_created_notion_tasks_do_not_request_hidden_kommo_write():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "app/agent/notion_gateway.py"
    ).read_text(encoding="utf-8")
    assert '"Обновить Kommo": {"checkbox": bool(update_kommo)}' in source
    assert "update_kommo: bool = False" in source
