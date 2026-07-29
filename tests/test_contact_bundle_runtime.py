from __future__ import annotations

import pytest

from app.services import contact_bundle_runtime


def _card(number: str, name: str, product: str, phone: str, email: str = "") -> dict:
    return {
        "lead_number": number,
        "name": name,
        "product": product,
        "phone": phone,
        "email": email,
    }


def test_contact_display_name_uses_number_name_and_product() -> None:
    card = _card("166", "przemek Bryłka", "Чай", "+48 698 136 090")
    assert (
        contact_bundle_runtime.contact_display_name(card)
        == "166 - Przemek Bryłka — Чай"
    )


def test_person_vcard_does_not_make_bbs_the_primary_name() -> None:
    card = _card(
        "166",
        "przemek Bryłka",
        "Чай",
        "+48 698 136 090",
        "niuppa1@yahoo.com",
    )
    text = contact_bundle_runtime.build_person_vcard(card).decode("utf-8")
    assert "FN:166 - Przemek Bryłka — Чай" in text
    assert "N:166 - Przemek Bryłka — Чай;;;;" in text
    assert "X-ABShowAs:PERSON" in text
    assert "ORG:B&BS" not in text
    assert "TEL;TYPE=CELL:+48698136090" in text


@pytest.mark.asyncio
async def test_multiple_contacts_are_sent_as_one_vcf(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict] = []

    async def fake_send_document(chat_id: int, **kwargs):
        sent.append({"chat_id": chat_id, **kwargs})
        return {"ok": True}

    monkeypatch.setattr(
        contact_bundle_runtime.telegram_service,
        "send_document",
        fake_send_document,
    )

    result = {
        "contact_cards": [
            _card("163", "Iza Gajewska", "воздушные шары", "+48 111 111 111"),
            _card("164", "Lewy Lewy", "Мини-экскаваторы", "+48 222 222 222"),
            _card("165", "Grzegorz Nowicki", "Солнечные панели", "+48 333 333 333"),
            _card("166", "przemek Bryłka", "Чай", "+48 698 136 090"),
        ]
    }

    await contact_bundle_runtime.send_vcards_bundled(123, result)

    assert len(sent) == 1
    assert sent[0]["filename"] == "BBS_contacts_163-166.vcf"
    assert sent[0]["mime_type"] == "text/vcard"
    content = sent[0]["content"].decode("utf-8")
    assert content.count("BEGIN:VCARD") == 4
    assert "FN:166 - Przemek Bryłka — Чай" in content
    assert "Контакты для iPhone · 4" in sent[0]["caption"]


@pytest.mark.asyncio
async def test_single_contact_keeps_single_contact_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict] = []

    async def fake_send_document(chat_id: int, **kwargs):
        sent.append({"chat_id": chat_id, **kwargs})
        return {"ok": True}

    monkeypatch.setattr(
        contact_bundle_runtime.telegram_service,
        "send_document",
        fake_send_document,
    )

    await contact_bundle_runtime.send_vcards_bundled(
        123,
        {
            "contact_cards": [
                _card("166", "przemek Bryłka", "Чай", "+48 698 136 090")
            ]
        },
    )

    assert len(sent) == 1
    assert sent[0]["filename"].endswith(".vcf")
    assert sent[0]["content"].decode("utf-8").count("BEGIN:VCARD") == 1
    assert "166 - Przemek Bryłka — Чай" in sent[0]["caption"]
