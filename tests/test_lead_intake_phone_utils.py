"""Reusable phone/email normalization (mandatory test case: 728387128 -> +48728387128)."""

from __future__ import annotations

from app.services import phone_utils


def test_polish_local_number_gets_country_code():
    assert phone_utils.normalize_phone("728387128") == "48728387128"
    assert phone_utils.to_e164("728387128") == "+48728387128"


def test_already_international_number_is_stable():
    assert phone_utils.normalize_phone("+48 728 387 128") == "48728387128"
    assert phone_utils.normalize_phone("0048-728-387-128") == "48728387128"
    assert phone_utils.normalize_phone("(48) 728 387 128") == "48728387128"


def test_local_and_international_forms_are_equal():
    assert phone_utils.phones_match("728387128", "+48 728 387 128")
    assert phone_utils.phones_match("728 387 128", "0048728387128")


def test_too_short_value_is_rejected():
    assert phone_utils.normalize_phone("12345") is None


def test_email_normalization_trims_and_lowercases():
    assert phone_utils.normalize_email("  Jan_Ovo@WP.pl  ") == "jan_ovo@wp.pl"
    assert phone_utils.emails_match("Jan_Ovo@WP.pl", "jan_ovo@wp.pl ")


def test_display_and_whatsapp_link():
    assert phone_utils.display_phone("728387128") == "+48 728 387 128"
    link = phone_utils.whatsapp_link("728387128", "Dzień dobry")
    assert link is not None
    assert link.startswith("https://wa.me/48728387128?text=")
