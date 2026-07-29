from app.models.kaizen_journal_entry import KaizenJournalEntry


def test_kaizen_model_enforces_type_status_source_and_period():
    names = {constraint.name for constraint in KaizenJournalEntry.__table__.constraints}
    assert "ck_kaizen_journal_entry_type" in names
    assert "ck_kaizen_journal_status" in names
    assert "ck_kaizen_journal_source" in names
    assert "ck_kaizen_journal_period_order" in names
    assert "uq_kaizen_journal_user_type_period" in names
