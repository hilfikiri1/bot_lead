from app.agent.digest import rank_lead


def test_overdue_task_is_top_priority():
    result = rank_lead({"closest_task_at": 900, "updated_at": 850}, now_ts=1000)
    assert result["priority"] == "Высокий"
    assert result["score"] >= 100


def test_no_task_is_high_priority():
    result = rank_lead({"closest_task_at": None, "updated_at": 900}, now_ts=1000)
    assert result["priority"] == "Высокий"
    assert "нет следующей задачи" in result["reason"]


def test_recent_lead_with_future_task_is_low_priority():
    result = rank_lead({"closest_task_at": 2000, "updated_at": 950}, now_ts=1000)
    assert result["priority"] == "Низкий"
