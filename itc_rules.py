"""
Deterministic Sec 17(5) ITC rules — invoice category + line-level classification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Any, Optional

GSTIN_REGEX = re.compile(
    r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$"
)

HARD_BLOCKED_CATEGORIES = {
    "Food & Beverages": (
        "17(5)(b)(i)",
        "Blocked under Section 17(5)(b)(i) - food, beverages, outdoor catering "
        "(unless used to make an outward taxable supply of the same category).",
    ),
    "Motor Vehicle & Cab Bookings": (
        "17(5)(a)",
        "Blocked under Section 17(5)(a) - passenger motor vehicles / cab bookings "
        "(<=13 seats) unless used for further taxable supply of transport / driving training.",
    ),
    "Club / Fitness / Wellness": (
        "17(5)(b)",
        "Blocked under Section 17(5)(b) - club, health and fitness centre memberships.",
    ),
    "Works Contract / Construction": (
        "17(5)(c)/(d)",
        "Blocked under Section 17(5)(c)/(d) - works contract / construction of "
        "immovable property (other than plant and machinery), unless for further works-contract supply.",
    ),
    "Personal Consumption": (
        "17(5)(g)",
        "Blocked under Section 17(5)(g) - goods or services used for personal consumption.",
    ),
    "Free Samples / Gifts / Write-off": (
        "17(5)(h)",
        "Blocked under Section 17(5)(h) - goods lost, stolen, destroyed, written off, "
        "or disposed of by way of gift or free samples.",
    ),
}

ALWAYS_ELIGIBLE_CATEGORIES = {
    "Office Supplies",
    "Electronics & Hardware",
    "Software & SaaS",
    "Professional & Consulting Services",
    "Rent & Infrastructure",
    "Logistics & Freight",
    "Raw Materials",
    "Marketing & Advertising",
    "Utilities (Electricity/Water/Internet)",
}

GRAY_CATEGORIES = {
    "Travel & Lodging",
    "Other",
}

_VACATION_HINTS = re.compile(
    r"\b(ltc|leave travel|vacation|holiday|tour|honeymoon|personal travel)\b",
    re.I,
)
_BUSINESS_TRAVEL_HINTS = re.compile(
    r"\b(client visit|business travel|conference|training|boarding|hotel stay|"
    r"lodging|accommodation|official travel)\b",
    re.I,
)

# Description / HSN → category for line-level ITC
_LINE_CATEGORY_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(catering|restaurant|lunch|dinner|beverage|food|meal|snack|tiffin)\b", re.I), "Food & Beverages"),
    (re.compile(r"\b(cab|taxi|uber|ola|sedan|motor\s*vehicle|car hire|chauffeur)\b", re.I), "Motor Vehicle & Cab Bookings"),
    (re.compile(r"\b(gym|fitness|club\s*membership|wellness|spa)\b", re.I), "Club / Fitness / Wellness"),
    (re.compile(r"\b(works\s*contract|construction|civil\s*work|immovable)\b", re.I), "Works Contract / Construction"),
    (re.compile(r"\b(free\s*sample|gift\s*hamper|complimentary|write[\s-]*off)\b", re.I), "Free Samples / Gifts / Write-off"),
    (re.compile(r"\b(personal\s*use|personal\s*consumption)\b", re.I), "Personal Consumption"),
    (re.compile(r"\b(hotel|lodging|boarding|guest\s*house|travel)\b", re.I), "Travel & Lodging"),
    (re.compile(r"\b(saas|software|subscription|license|cloud)\b", re.I), "Software & SaaS"),
    (re.compile(r"\b(laptop|monitor|printer|toner|drum|usb|ethernet|router|keyboard|mouse|ssd|hdd)\b", re.I), "Electronics & Hardware"),
    (re.compile(r"\b(paper|stapler|pen|folder|toner|stationery|desk|whiteboard|organizer)\b", re.I), "Office Supplies"),
    (re.compile(r"\b(freight|courier|logistics|shipping|transport\s*of\s*goods)\b", re.I), "Logistics & Freight"),
    (re.compile(r"\b(raw\s*material|steel|cement|chemical|fabric)\b", re.I), "Raw Materials"),
    (re.compile(r"\b(rent|lease|warehouse)\b", re.I), "Rent & Infrastructure"),
    (re.compile(r"\b(electricity|water|internet|broadband|utility)\b", re.I), "Utilities (Electricity/Water/Internet)"),
    (re.compile(r"\b(advertis|marketing|promo)\b", re.I), "Marketing & Advertising"),
    (re.compile(r"\b(consult|professional\s*fee|audit\s*fee|legal\s*fee)\b", re.I), "Professional & Consulting Services"),
]

# SAC/HSN prefixes commonly blocked / suggestive
_HSN_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^9963"), "Food & Beverages"),       # catering / restaurant-ish SAC
    (re.compile(r"^9964"), "Motor Vehicle & Cab Bookings"),  # passenger transport
    (re.compile(r"^9972"), "Rent & Infrastructure"),
    (re.compile(r"^9983"), "Professional & Consulting Services"),
    (re.compile(r"^9984"), "Software & SaaS"),
]


@dataclass
class ItcDecision:
    is_eligible: bool
    reason: Optional[str]
    rule_code: Optional[str]
    source: str  # 'rule' | 'gray' | 'unregistered' | 'line'
    needs_llm: bool = False

    def reason_with_code(self) -> Optional[str]:
        if self.is_eligible:
            return None
        if self.rule_code and self.reason:
            if self.reason.startswith(f"[{self.rule_code}]"):
                return self.reason
            return f"[{self.rule_code}] {self.reason}"
        return self.reason


def validate_gstin(gstin: Optional[str]) -> bool:
    if not gstin:
        return False
    return bool(GSTIN_REGEX.match(str(gstin).strip().upper()))


def normalize_gstin(gstin: Optional[str]) -> str:
    return (gstin or "").strip().upper()


def apply_deterministic_itc_rules(
    category: Optional[str],
    *,
    is_recipient_registered: bool,
    line_items_summary: str = "",
) -> ItcDecision:
    """Invoice/category-level rules (used as fallback and for line inferred categories)."""
    if not is_recipient_registered:
        return ItcDecision(
            is_eligible=False,
            reason="Recipient is not a registered GST holder (B2C / invalid GSTIN - ITC not available).",
            rule_code="sec16-registration",
            source="unregistered",
            needs_llm=False,
        )

    cat = (category or "Other").strip()
    summary = line_items_summary or ""

    if cat in HARD_BLOCKED_CATEGORIES:
        code, reason = HARD_BLOCKED_CATEGORIES[cat]
        return ItcDecision(
            is_eligible=False,
            reason=reason,
            rule_code=code,
            source="rule",
            needs_llm=False,
        )

    if cat in ALWAYS_ELIGIBLE_CATEGORIES:
        return ItcDecision(
            is_eligible=True,
            reason=None,
            rule_code="eligible-business-input",
            source="rule",
            needs_llm=False,
        )

    if cat == "Travel & Lodging":
        if _VACATION_HINTS.search(summary):
            return ItcDecision(
                is_eligible=False,
                reason="Blocked under Section 17(5)(b) - travel benefits to employees on vacation (LTC / personal holiday).",
                rule_code="17(5)(b)-ltc",
                source="rule",
                needs_llm=False,
            )
        if _BUSINESS_TRAVEL_HINTS.search(summary):
            return ItcDecision(
                is_eligible=True,
                reason=None,
                rule_code="travel-business-provisional",
                source="rule",
                needs_llm=False,
            )
        return ItcDecision(
            is_eligible=False,
            reason="Travel & Lodging needs review - confirm business lodging vs employee vacation benefits (Sec 17(5)(b)).",
            rule_code="17(5)(b)-travel-review",
            source="gray",
            needs_llm=True,
        )

    if cat in GRAY_CATEGORIES or cat == "Other":
        return ItcDecision(
            is_eligible=True,
            reason=None,
            rule_code="gray-needs-llm",
            source="gray",
            needs_llm=True,
        )

    return ItcDecision(
        is_eligible=True,
        reason=None,
        rule_code="unknown-category",
        source="gray",
        needs_llm=True,
    )


def infer_line_category(
    description: str = "",
    hsn_or_sac: str = "",
    invoice_category: str = "Other",
) -> str:
    """Infer a Sec 17(5)-oriented category for one line item."""
    text = f"{description or ''} {hsn_or_sac or ''}".strip()
    for pattern, cat in _LINE_CATEGORY_RULES:
        if pattern.search(text):
            return cat
    hsn = (hsn_or_sac or "").strip()
    for pattern, cat in _HSN_HINTS:
        if pattern.search(hsn):
            return cat
    inv = (invoice_category or "Other").strip()
    if inv and inv != "Other":
        return inv
    return "Other"


def classify_line_item(
    *,
    description: str = "",
    hsn_or_sac: str = "",
    invoice_category: str = "Other",
    is_recipient_registered: bool = True,
) -> ItcDecision:
    """Classify a single line for ITC (deterministic; no LLM)."""
    if not is_recipient_registered:
        return ItcDecision(
            is_eligible=False,
            reason="Recipient is not a registered GST holder (B2C / invalid GSTIN - ITC not available).",
            rule_code="sec16-registration",
            source="unregistered",
            needs_llm=False,
        )

    line_cat = infer_line_category(description, hsn_or_sac, invoice_category)
    decision = apply_deterministic_itc_rules(
        line_cat,
        is_recipient_registered=True,
        line_items_summary=description or "",
    )
    # For gray "Other" lines with no strong signal, default eligible (business input presumption)
    # instead of forcing LLM per line (keeps complex bills fast + deterministic).
    if decision.needs_llm and line_cat == "Other":
        return ItcDecision(
            is_eligible=True,
            reason=None,
            rule_code="line-presumed-eligible",
            source="line",
            needs_llm=False,
        )
    decision.source = "line"
    decision.needs_llm = False  # line path stays deterministic for product reliability
    return decision


def _line_gst(item: dict) -> tuple[float, float, float]:
    return (
        float(item.get("cgst") or 0),
        float(item.get("sgst") or 0),
        float(item.get("igst") or 0),
    )


def evaluate_line_items_itc(
    line_items: list[dict] | None,
    *,
    invoice_category: str = "Other",
    is_recipient_registered: bool = True,
) -> dict[str, Any]:
    """
    Line-level ITC evaluation for complex / mixed invoices.

    Returns rollups used by metrics:
      - eligible_cgst/sgst/igst, blocked_gst
      - is_itc_eligible (True if any line eligible)
      - itc_ineligibility_reason (summary of blocked lines, if any)
      - lines: enriched line dicts with ITC fields
    """
    items = line_items or []
    enriched: list[dict] = []
    elig_c = elig_s = elig_i = 0.0
    blocked = 0.0
    blocked_notes: list[str] = []

    if not items:
        # No lines — fall back to invoice category
        d = apply_deterministic_itc_rules(
            invoice_category,
            is_recipient_registered=is_recipient_registered,
        )
        return {
            "lines": [],
            "eligible_cgst": 0.0,
            "eligible_sgst": 0.0,
            "eligible_igst": 0.0,
            "blocked_gst": 0.0,
            "is_itc_eligible": d.is_eligible,
            "itc_ineligibility_reason": d.reason_with_code(),
            "partial": False,
        }

    for raw in items:
        item = dict(raw or {})
        decision = classify_line_item(
            description=str(item.get("description") or ""),
            hsn_or_sac=str(item.get("hsn_or_sac") or ""),
            invoice_category=invoice_category,
            is_recipient_registered=is_recipient_registered,
        )
        cgst, sgst, igst = _line_gst(item)
        gst = cgst + sgst + igst
        item["is_itc_eligible"] = decision.is_eligible
        item["itc_ineligibility_reason"] = decision.reason_with_code()
        item["itc_rule_code"] = decision.rule_code
        item["inferred_category"] = infer_line_category(
            str(item.get("description") or ""),
            str(item.get("hsn_or_sac") or ""),
            invoice_category,
        )
        if decision.is_eligible:
            elig_c += cgst
            elig_s += sgst
            elig_i += igst
        else:
            blocked += gst
            desc = (item.get("description") or "line")[:60]
            blocked_notes.append(f"{desc}: {decision.reason_with_code()}")
        enriched.append(item)

    eligible_gst = elig_c + elig_s + elig_i
    partial = eligible_gst > 0 and blocked > 0
    any_eligible = eligible_gst > 0

    if not is_recipient_registered:
        reason = "Recipient is not a registered GST holder (B2C / invalid GSTIN - ITC not available)."
    elif blocked_notes and not any_eligible:
        reason = "All lines blocked under Sec 17(5). " + " | ".join(blocked_notes[:3])
    elif partial:
        reason = (
            f"Partial ITC: {len([x for x in enriched if x.get('is_itc_eligible')])} eligible / "
            f"{len([x for x in enriched if not x.get('is_itc_eligible')])} blocked lines. "
            + " | ".join(blocked_notes[:3])
        )
    else:
        reason = None

    return {
        "lines": enriched,
        "eligible_cgst": round(elig_c, 2),
        "eligible_sgst": round(elig_s, 2),
        "eligible_igst": round(elig_i, 2),
        "blocked_gst": round(blocked, 2),
        "is_itc_eligible": any_eligible if is_recipient_registered else False,
        "itc_ineligibility_reason": reason,
        "partial": partial,
    }
