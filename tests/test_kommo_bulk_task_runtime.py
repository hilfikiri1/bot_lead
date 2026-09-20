from __future__ import annotations

from app.services import kommo_bulk_json_runtime as bulk
from app.services import kommo_bulk_task_runtime as task_runtime


def test_bulk_task_validation_accepts_create_task():
    task_runtime.install_kommo_bulk_task_runtime()

    payload = bulk.validate_bulk_payload(
        {
            "version": 1,
            "actions": [
                {
                    "lead_ref": {"kommo_id": 123456},
                    "action": "create_task",
                    "task": {
                        "text": "Позвонить клиенту и актуализировать запрос",
                        "due_at": "2099-09-21 10:00",
                    },
                }
            ],
        }
    )

    item = payload["actions"][0]
    assert item["action"] == "create_task"
    assert item["lead_ref"] == {"kommo_id": 123456}
    assert item["task"]["text"].startswith("Позвонить клиенту")
    assert item["task"]["due_at"] == "2099-09-21 10:00"


def test_bulk_task_validation_rejects_task_on_keep():
    task_runtime.install_kommo_bulk_task_runtime()

    try:
        bulk.validate_bulk_payload(
            {
                "version": 1,
                "actions": [
                    {
                        "lead_ref": {"kommo_id": 123456},
                        "action": "keep",
                        "task": {
                            "text": "Позвонить",
                            "due_at": "2099-09-21 10:00",
                        },
                    }
                ],
            }
        )
    except ValueError as exc:
        assert "только для action=create_task" in str(exc)
    else:
        raise AssertionError("Expected task-on-keep validation error")
