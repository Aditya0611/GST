"""Verify line-level ITC on the complex mixed bill pattern."""
from itc_rules import evaluate_line_items_itc, classify_line_item

LINES = [
    {"description": "A4 Copier Paper Ream 75GSM", "hsn_or_sac": "4802", "cgst": 225.0, "sgst": 225.0, "igst": 0},
    {"description": "USB-C Hub Multiport", "hsn_or_sac": "8471", "cgst": 486.0, "sgst": 486.0, "igst": 0},
    {"description": "Team Lunch Outdoor Catering", "hsn_or_sac": "9963", "cgst": 150.0, "sgst": 150.0, "igst": 0},
    {"description": "Airport Cab Booking Sedan", "hsn_or_sac": "9964", "cgst": 45.0, "sgst": 45.0, "igst": 0},
]


def test_line_classify_food_blocked():
    d = classify_line_item(description="Team Lunch Outdoor Catering", hsn_or_sac="9963")
    assert d.is_eligible is False


def test_line_classify_office_eligible():
    d = classify_line_item(description="A4 Copier Paper Ream", hsn_or_sac="4802")
    assert d.is_eligible is True


def test_mixed_invoice_partial():
    result = evaluate_line_items_itc(LINES, invoice_category="Other", is_recipient_registered=True)
    assert result["partial"] is True
    assert result["is_itc_eligible"] is True
    # paper 450 + hub 972 = 1422 eligible; catering 300 + cab 90 = 390 blocked
    assert abs(result["eligible_cgst"] + result["eligible_sgst"] + result["eligible_igst"] - 1422.0) < 0.01
    assert abs(result["blocked_gst"] - 390.0) < 0.01


if __name__ == "__main__":
    test_line_classify_food_blocked()
    test_line_classify_office_eligible()
    test_mixed_invoice_partial()
    print("line-level ITC unit tests passed")
