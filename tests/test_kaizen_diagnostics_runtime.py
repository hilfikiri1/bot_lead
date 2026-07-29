from app.services import kaizen_diagnostics_runtime, system_diagnostics


def test_kaizen_table_is_required_by_diag():
    kaizen_diagnostics_runtime.install_kaizen_diagnostics_runtime()
    assert "kaizen_journal_entries" in system_diagnostics._REQUIRED_TABLES
    assert system_diagnostics._REQUIRED_TABLES.count("kaizen_journal_entries") == 1
