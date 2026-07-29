import pytest

from app.agent import tools


@pytest.mark.asyncio
async def test_explicit_query_overrides_stale_active_lead(monkeypatch):
    calls = []

    async def fake_search(query, limit):
        calls.append(("search", query, limit))
        return {"leads": [{"id": 222, "name": "135 - кормушки"}]}

    async def fake_details(lead_id):
        calls.append(("details", lead_id))
        return {"id": lead_id, "name": "135 - кормушки"}

    monkeypatch.setattr(tools.kommo_service, "search_open_leads", fake_search)
    monkeypatch.setattr(tools.kommo_service, "get_lead_details", fake_details)

    lead = await tools.resolve_lead(
        lead_id=None,
        query="Покажи сделку по кормушкам",
        context={"active_kommo_lead_id": 111},
    )

    assert lead["id"] == 222
    assert calls == [("search", "кормушк", 8), ("details", 222)]


@pytest.mark.asyncio
async def test_active_lead_is_used_when_no_new_reference(monkeypatch):
    calls = []

    async def fake_details(lead_id):
        calls.append(lead_id)
        return {"id": lead_id}

    monkeypatch.setattr(tools.kommo_service, "get_lead_details", fake_details)

    lead = await tools.resolve_lead(
        lead_id=None,
        query=None,
        context={"active_kommo_lead_id": 111},
    )

    assert lead["id"] == 111
    assert calls == [111]
