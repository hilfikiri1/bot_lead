from app.services.digest_rules import rank_lead


def test_overdue_task_is_high_priority():
    result = rank_lead({"closest_task_at": 900, "updated_at": 800}, now_ts=1000)
    assert result["priority"] == "Высокий"
    assert result["task_type"] == "Звонок"


def test_missing_next_task_is_high_priority():
    result = rank_lead({"closest_task_at": None, "updated_at": 900}, now_ts=1000)
    assert result["priority"] == "Высокий"
    assert "нет следующей задачи" in result["reason"]
