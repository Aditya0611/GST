# Pricing model shape (Taxova.ai)

**Decision (product, not billing launch):** default commercial shape is **per-firm flat subscription**.

The `firms` table is the paying tenant. Seats and invoice volume are **optional caps** for later plan tiers — not the primary meter until we have usage data.

## Public plans (locked)

| Plan | Price | Who | Typical caps |
|------|--------|-----|----------------|
| **Starter** | **₹999 / month** | Single business | 1 GSTIN / WhatsApp client |
| **Growth** | **₹1,999 / month** | CA firms / growing practices | ~25–100 clients (set at signup) |

WhatsApp intake, AI extract, ITC checks, and CA dashboard are in both. Growth adds multi-client workspace, 2B, GSTR draft exports, and priority support.

Prices are **indicative INR**; GST extra unless stated. No checkout yet — website CTA is WhatsApp / demo.

## Why per-firm flat

- One CA practice = one workspace, many clients (matches current tenancy).
- Simple sales story for Indian CA firms (monthly/annual firm fee).
- Avoids metering every WhatsApp invoice on day one (support + disputes).
- Seat / invoice caps can still gate Starter vs Growth without changing the tenant key.

## Schema (nullable / soft until billing ships)

| Column | Meaning |
|--------|---------|
| `billing_model` | `'per_firm'` (default) \| `'per_seat'` \| `'per_invoice_volume'` |
| `plan_tier` | `trial` / `starter` / `growth` (free text until billing ships) |
| `seat_limit` | Max CA logins under the firm (null = unlimited) |
| `monthly_invoice_cap` | Soft/hard cap on invoices processed per calendar month |
| `client_cap` | Max linked business clients |
| `billing_status` | `trial` \| `active` \| `past_due` \| `cancelled` |
| `trial_ends_at` | Optional trial end |

New firms get `billing_model=per_firm`, `billing_status=trial`.

## Live checkout (Razorpay)

Public page: `/checkout?plan=starter|growth`  
APIs: `POST /api/billing/create-order`, `POST /api/billing/verify`  

Requires env: `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`. Use Live keys for production.

On successful payment Taxova creates a firm + first CA invite and sets `billing_status=active` with plan caps.

## Not in scope yet

- Stripe
- Enforcing caps in every API (soft until post-launch)
- Per-invoice overage pricing
- Automatic renewals (manual / Razorpay subscriptions can follow)

## If we pivot later

- **Per-seat:** set `billing_model=per_seat` and enforce `seat_limit` on CA create.
- **Per-invoice volume:** set `billing_model=per_invoice_volume` and count against `monthly_invoice_cap`.

Do not invent a second tenant table — keep billing on `firms`.
