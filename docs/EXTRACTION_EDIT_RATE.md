# Extraction edit-rate logging

**Goal:** measure whether AI invoice extraction is good enough using real CA traffic, before a labeled photo benchmark.

## What is logged

When a CA **edits** an extraction-sourced field (`PUT /api/invoices/{id}`), Taxova writes a row to `extraction_field_edits`:

- `invoice_id`, `firm_id`, `client_phone`
- `field_name`, `old_value`, `new_value`
- `ca_user`, `ca_invite_code`, `created_at`

Tracked fields: supplier/recipient name & GSTIN, invoice number/date, place of supply, taxable & tax totals, grand total, business category, supply type.

The invoice row also rolls up:

- `extraction_edit_count` — distinct extraction fields edited so far
- `extraction_edited_fields` — JSON list of those field names

On **approve**, `invoice_extraction_outcomes` stores whether that invoice had any extraction edits (the unit for edit-rate).

## Primary metric

```
edit_rate = approved_with_extraction_edits / approved_invoices
```

Lower is better. Field breakdown shows where models fail (e.g. `supplier_gstin` vs `grand_total`).

## What counts as an “outcome” (interpret carefully)

| CA action | Field edits logged to `extraction_field_edits`? | Row in `invoice_extraction_outcomes`? |
|-----------|-----------------------------------------------|----------------------------------------|
| **Edit** (save fields) | Yes (per extraction field) | No |
| **Approve** | (prior edits already logged) | **Yes** — this is what `edit_rate` uses |
| **Skip** | Only if they edited before skip | **No** |
| **Reject** | Only if they edited before reject | **No** |

So **`edit_rate` is “among invoices the CA approved, what share needed extraction fixes.”**  
Skipped/rejected bills with bad extraction do **not** enter the denominator. If CAs reject garbage photos instead of fixing fields, edit_rate will look better than reality — pair it with reject/skip volume later.

## API (platform admin)

```
GET /api/admin/extraction-edit-stats?days=30
GET /api/admin/extraction-edit-stats?days=30&firm_id=2
```

Requires `DASHBOARD_API_KEY` (`X-API-Key`).

## Note

ITC / review-status toggles are **not** counted as extraction errors — only fields the model extracted from the bill image/PDF.
