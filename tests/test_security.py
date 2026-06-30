import pytest
from fastapi import HTTPException


def test_admin_api_disabled_without_key(monkeypatch):
    from app.api import admin

    monkeypatch.setattr(admin.settings, "admin_api_key", "")
    with pytest.raises(HTTPException) as exc:
        admin.require_admin_key(None)
    assert exc.value.status_code == 503


def test_admin_api_rejects_wrong_key(monkeypatch):
    from app.api import admin

    monkeypatch.setattr(admin.settings, "admin_api_key", "correct")
    with pytest.raises(HTTPException) as exc:
        admin.require_admin_key("wrong")
    assert exc.value.status_code == 401


def test_admin_api_accepts_correct_key(monkeypatch):
    from app.api import admin

    monkeypatch.setattr(admin.settings, "admin_api_key", "correct")
    assert admin.require_admin_key("correct") is None
