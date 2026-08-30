# Controlled pilot criteria (locked before data)

Reset smoke-test edit-rate rows before any real firm traffic so week-1 numbers are clean.

## Firm selection

- One warm-intro CA firm (tolerates early bugs).
- Not a cold sales prospect for v1 signal.

## Volume bar (do not trust `edit_rate` before this)

| Gate | Rule |
|------|------|
| **Minimum** | **≥ 25 approved invoices** in `invoice_extraction_outcomes` |
| Soft watch | After 10 approved, glance at `by_field` only — no go/no-go decision |
| Tea-leaf ban | Do not interpret `edit_rate` with fewer than 25 approvals |

Query: `GET /api/admin/extraction-edit-stats?days=30` → use `approved_invoices`.

## Relationship check-in (not a metrics gate)

At **~10 approved invoices**, message the pilot CA: *“How’s this feeling so far?”* — listen for extraction pain, UI confusion, or portal flakiness. Do **not** quote `edit_rate` or make a scale/hold call. Goal is to keep the relationship alive through a rough week-one streak before the 25-approval bar.

## What to watch

1. **Aggregate `edit_rate`** — share of approved invoices with ≥1 extraction-field edit.
2. **`by_field`**, especially filing-critical:
   - `supplier_gstin`, `recipient_gstin`
   - `grand_total`, `total_taxable_value`, `total_cgst`, `total_sgst`, `total_igst`
   - `invoice_number`, `invoice_date`
3. Cosmetic / softer: `supplier_name`, `recipient_name`, `place_of_supply`, `business_category`.

A high rate only on names is annoying; a high rate on GSTIN / totals blocks filing confidence.

## Pre-committed “good enough” bar (decide before seeing pilot data)

| Signal | Go (scale outreach) | Hold (fix extraction first) |
|--------|---------------------|------------------------------|
| Filing-critical field edit rate* | **&lt; 15%** of approved invoices need an edit on that field | **≥ 15%** on any of GSTIN / taxable / tax / grand_total |
| Overall `edit_rate` | **&lt; 25%** | **≥ 25%** |
| Volume | Met ≥25 approvals | Below bar → keep collecting |

\*Approximate from `by_field[].invoices / approved_invoices` for that field.

**Outcome interpretation reminder:** only **approve** writes outcomes. Skip/reject with bad extraction do not inflate `edit_rate` — also track reject/skip counts weekly.

## Sandbox / portal for the pilot

- Live GSTIN lookup / Pull 2B may fail if Sandbox subscription lapses.
- UX copy already frames this as **portal offline, not Taxova broken**; CAs should use **Import GSTR-2B** + manual GSTIN.
- Renew Sandbox before pilot if you want live lookup; not a blocker for review-queue learning.

## Pre-flight checklist

- [x] Smoke `extraction_field_edits` / `invoice_extraction_outcomes` cleared (pilot day zero)
- [ ] Warm CA firm + invite created (`POST /api/admin/firms`)
- [ ] Clients linked via `/ca-assign`
- [ ] CA trained: Sign out vs Settings login; Import 2B if portal offline
- [ ] Schedule week-1 review of stats at ≥25 approvals
- [ ] Calendar/reminder: human check-in at ~10 approvals (“how’s this feeling?”)
