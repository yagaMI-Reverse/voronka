import pytest

from voronka.keys import dedup_key, normalize_email, normalize_phone


@pytest.mark.parametrize(
    "raw",
    [
        "+7 707 123-45-67",
        "8 (707) 123 45 67",
        "87071234567",
        "+77071234567",
        "7071234567",
    ],
)
def test_phone_variants_collapse(raw):
    assert normalize_phone(raw) == "77071234567"


def test_phone_empty():
    assert normalize_phone("") == ""
    assert normalize_phone("не указан") == ""


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Ilya@Example.COM ", "ilya@example.com"),
        ("i.l.y.a+hh@gmail.com", "ilya@gmail.com"),
        ("ilya@googlemail.com", "ilya@gmail.com"),
        ("нет почты", ""),
    ],
)
def test_email_normalization(raw, expected):
    assert normalize_email(raw) == expected


def test_same_person_same_key():
    a = dedup_key(source="lead", phone="+7 707 123-45-67", form_id="landing")
    b = dedup_key(source="lead", phone="87071234567", form_id="landing")
    assert a == b


def test_phone_wins_over_email():
    """Разный email при одном телефоне — тот же человек."""
    a = dedup_key(source="lead", phone="77071234567", email="a@x.com", form_id="landing")
    b = dedup_key(source="lead", phone="77071234567", email="b@x.com", form_id="landing")
    assert a == b


def test_different_forms_are_different_leads():
    a = dedup_key(source="lead", phone="77071234567", form_id="landing")
    b = dedup_key(source="lead", phone="77071234567", form_id="webinar")
    assert a != b


def test_no_contact_is_an_error():
    with pytest.raises(ValueError):
        dedup_key(source="lead", phone="", email="", telegram_id="")
