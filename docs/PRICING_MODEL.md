# Pricing model shape (Taxova.ai)

**Decision (product, not billing launch):** default commercial shape is **per-firm flat subscription**.

The `firms` table is the paying tenant. Seats and invoice volume are **optional caps** for later plan tiers — not the primary meter until we have usage data.

## Why per-firm flat

- One CA practice = one workspace, many clients (matches current tenancy).
- Simple sales story for Indian CA firms (monthly/annual firm fee).
- Avoids metering every WhatsApp invoice on day one (support + disputes).
- Seat / invoice caps can still gate “Growth / Scale” without changing the tenant key.

## Schema (nullable / soft until billing ships)

| Column | Meaning |
|--------|---------|
| `billing_model` | `'per_firm'` (default) \| `'per_seat'` \| `'per_invoice_volume'` |
| `plan_tier` | e.g. `trial` / `starter` / `growth` / `scale` (free text for now) |
| `seat_limit` | Max CA logins under the firm (null = unlimited) |
| `monthly_invoice_cap` | Soft/hard cap on invoices processed per calendar month |
| `client_cap` | Max linked business clients |
| `billing_status` | `trial` \| `active` \| `past_due` \| `cancelled` |
| `trial_ends_at` | Optional trial end |

New firms get `billing_model=per_firm`, `billing_status=trial`.

## Not in scope yet

- Stripe / Razorpay / invoices
- Enforcing caps in API (add when first paid plan goes live)
- Per-invoice overage pricing

## If we pivot later

- **Per-seat:** set `billing_model=per_seat` and enforce `seat_limit` on CA create.
- **Per-invoice volume:** set `billing_model=per_invoice_volume` and count against `monthly_invoice_cap`.

Do not invent a second tenant table — keep billing on `firms`.
