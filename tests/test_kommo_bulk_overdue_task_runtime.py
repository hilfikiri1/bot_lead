from app.services.kommo_bulk_overdue_task_runtime import _overdue_tasks


def test_overdue_tasks_keeps_only_open_past_due_items():
    tasks = [
        {"id": 1, "complete_till": 100, "is_completed": False},
        {"id": 2, "complete_till": 200, "is_completed": False},
        {"id": 3, "complete_till": 99, "is_completed": True},
        {"id": 4, "complete_till": None, "is_completed": False},
    ]

    result = _overdue_tasks(tasks, now_ts=150)

    assert [item["id"] for item in result] == [1]


def test_overdue_tasks_does_not_treat_deadline_now_as_overdue():
    tasks = [{"id": 10, "complete_till": 150, "is_completed": False}]

    assert _overdue_tasks(tasks, now_ts=150) == []
