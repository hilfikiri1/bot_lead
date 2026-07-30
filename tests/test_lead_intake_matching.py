"""Kommo <-> Google Sheets matching priority (mandatory cases 1-9)."""

from __future__ import annotations

from app.services.lead_intake import matching
from app.services.lead_intake.matching import LeadSnapshot
from tests.lead_intake_helpers import make_row


def _snapshot(**overrides):
    base = dict(
        facebook_lead_id="12312412",
        phone="728387128",
        email="jan_ovo@wp.pl",
        name="Andrzej Janka",
        product="narzędzia",
        region="kujawsko pomorskie",
    )
    base.update(overrides)
    return LeadSnapshot(**base)


def test_case1_exact_facebook_lead_id_match():
    rows = [
        make_row(row_number=167, facebook_lead_id="12312412", phone=None, email=None),
        make_row(row_number=200, facebook_lead_id="999", phone="500000000"),
    ]
    outcome = matching.match_lead(_snapshot(), rows)
    assert outcome.status == "matched"
    assert outcome.method == "facebook_lead_id"
    assert outcome.row.row_number == 167


def test_case2_phone_and_email_match():
    rows = [
        make_row(row_number=167, phone="+48 728 387 128", email="jan_ovo@wp.pl"),
        make_row(row_number=200, phone="600111222", email="other@example.com"),
    ]
    outcome = matching.match_lead(_snapshot(facebook_lead_id=None), rows)
    assert outcome.status == "matched"
    assert outcome.method == "phone_and_email"
    assert outcome.row.row_number == 167


def test_case3_unique_phone_match():
    rows = [
        make_row(row_number=167, phone="728387128", email="different@example.com"),
        make_row(row_number=200, phone="600111222", email="other@example.com"),
    ]
    outcome = matching.match_lead(_snapshot(facebook_lead_id=None, email="unknown@example.com"), rows)
    # Emails differ, so priority 2 fails; unique phone still resolves it.
    assert outcome.status == "matched"
    assert outcome.method == "phone"
    assert outcome.row.row_number == 167


def test_case4_unique_email_match():
    rows = [
        make_row(row_number=167, phone="600999999", email="jan_ovo@wp.pl"),
        make_row(row_number=200, phone="600111222", email="other@example.com"),
    ]
    outcome = matching.match_lead(_snapshot(facebook_lead_id=None, phone="000000000"), rows)
    assert outcome.status == "matched"
    assert outcome.method == "email"
    assert outcome.row.row_number == 167


def test_case5_duplicate_phone_requires_manual_selection():
    rows = [
        make_row(row_number=167, phone="728387128", product="narzędzia"),
        make_row(row_number=181, phone="728387128", product="elektronarzędzia"),
    ]
    outcome = matching.match_lead(_snapshot(facebook_lead_id=None, email=None), rows)
    assert outcome.status == "ambiguous"
    assert outcome.reason == matching.REASON_DUPLICATE_PHONE
    assert {row.row_number for row in outcome.candidates} == {167, 181}


def test_case6_duplicate_email_requires_manual_selection():
    rows = [
        make_row(row_number=167, phone="600000001", email="jan_ovo@wp.pl"),
        make_row(row_number=181, phone="600000002", email="jan_ovo@wp.pl"),
    ]
    outcome = matching.match_lead(
        _snapshot(facebook_lead_id=None, phone="000000000", email="jan_ovo@wp.pl"), rows
    )
    assert outcome.status == "ambiguous"
    assert outcome.reason == matching.REASON_DUPLICATE_EMAIL


def test_case7_no_matching_row():
    rows = [make_row(row_number=1, phone="600000001", email="a@example.com")]
    outcome = matching.match_lead(
        _snapshot(facebook_lead_id=None, phone="000000000", email="unknown@example.com"), rows
    )
    assert outcome.status == "not_found"
    assert outcome.reason == matching.REASON_NO_MATCHING_ROW


def test_case8_row_already_has_internal_id_is_reused_not_reassigned():
    rows = [make_row(row_number=167, phone="728387128", lead_number="167")]
    outcome = matching.match_lead(_snapshot(facebook_lead_id=None, email=None), rows)
    assert outcome.status == "matched"
    assert outcome.row.lead_number == "167"


def test_product_is_never_a_unique_matching_key():
    rows = [
        make_row(row_number=1, phone=None, email=None, product="narzędzia"),
        make_row(row_number=2, phone=None, email=None, product="narzędzia"),
    ]
    outcome = matching.match_lead(
        LeadSnapshot(facebook_lead_id=None, phone=None, email=None, product="narzędzia"), rows
    )
    assert outcome.status == "not_found"
    assert outcome.reason == matching.REASON_MISSING_REQUIRED_FIELDS


def test_missing_required_fields_when_lead_has_no_identifiers():
    outcome = matching.match_lead(
        LeadSnapshot(facebook_lead_id=None, phone=None, email=None), [make_row(row_number=1)]
    )
    assert outcome.status == "not_found"
    assert outcome.reason == matching.REASON_MISSING_REQUIRED_FIELDS


def test_conflicting_facebook_id_across_rows_is_ambiguous():
    rows = [
        make_row(row_number=1, facebook_lead_id="12312412", phone=None, email=None),
        make_row(row_number=2, facebook_lead_id="12312412", phone=None, email=None),
    ]
    outcome = matching.match_lead(_snapshot(phone=None, email=None), rows)
    assert outcome.status == "ambiguous"
    assert outcome.reason == matching.REASON_CONFLICTING_FACEBOOK_ID
