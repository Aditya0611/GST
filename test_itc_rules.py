"""Unit tests for deterministic Sec 17(5) ITC rules."""
from itc_rules import apply_deterministic_itc_rules, validate_gstin


def test_validate_gstin():
    assert validate_gstin("27AAPFU0939F1ZV")
    assert not validate_gstin("YGVHBJKNLKUHVUV")
    assert not validate_gstin("")
    assert not validate_gstin(None)


def test_food_blocked():
    d = apply_deterministic_itc_rules("Food & Beverages", is_recipient_registered=True)
    assert d.is_eligible is False
    assert d.needs_llm is False
    assert "17(5)(b)(i)" in (d.rule_code or "")


def test_office_eligible():
    d = apply_deterministic_itc_rules("Office Supplies", is_recipient_registered=True)
    assert d.is_eligible is True
    assert d.needs_llm is False


def test_unregistered_blocked():
    d = apply_deterministic_itc_rules("Office Supplies", is_recipient_registered=False)
    assert d.is_eligible is False
    assert d.source == "unregistered"


def test_travel_vacation_blocked():
    d = apply_deterministic_itc_rules(
        "Travel & Lodging",
        is_recipient_registered=True,
        line_items_summary="Employee LTC vacation package",
    )
    assert d.is_eligible is False
    assert d.needs_llm is False


def test_travel_business_eligible():
    d = apply_deterministic_itc_rules(
        "Travel & Lodging",
        is_recipient_registered=True,
        line_items_summary="Hotel stay for client visit conference",
    )
    assert d.is_eligible is True
    assert d.needs_llm is False


def test_electronics_not_llm():
    d = apply_deterministic_itc_rules("Electronics & Hardware", is_recipient_registered=True)
    assert d.is_eligible is True
    assert d.needs_llm is False


if __name__ == "__main__":
    test_validate_gstin()
    test_food_blocked()
    test_office_eligible()
    test_unregistered_blocked()
    test_travel_vacation_blocked()
    test_travel_business_eligible()
    test_electronics_not_llm()
    print("All itc_rules tests passed.")
