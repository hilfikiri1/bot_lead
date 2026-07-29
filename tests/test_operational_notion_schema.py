from app.services import operational_notion_service as notion


def test_required_project_schema_uses_new_russian_properties():
    assert notion.PROJECT_SCHEMA["Название"] == "title"
    assert notion.PROJECT_SCHEMA["Kommo ID"] == "number"
    assert notion.PROJECT_SCHEMA["Родительский клиент"] == "relation"
    assert "Name" not in notion.PROJECT_SCHEMA


def test_required_task_schema_has_dedup_and_sync_fields():
    assert notion.TASK_SCHEMA["External ID"] == "rich_text"
    assert notion.TASK_SCHEMA["Sync status"] == "select"
    assert notion.TASK_SCHEMA["Обновить Kommo"] == "checkbox"
