# GST pilot runbook — execute to 25 approvals

**Goal:** Get one warm CA firm to **≥ 25 approved invoices** with clean metrics.  
**Do not build richer AIS until this gate clears** (go/hold in `docs/PILOT_CRITERIA.md`).

Live dashboard: GST tab → **GST pilot** panel → **Review next**.

---

## Day zero (before real traffic)

1. **Reset smoke metrics** (platform admin API key only):

```bash
curl -X POST "https://taxova.pro/api/admin/extraction-edit-stats/reset" \
  -H "X-API-Key: $DASHBOARD_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"confirm\":\"RESET_EDIT_STATS\"}"
```

Optional firm scope: add `"firm_id": 2` to the JSON body.

2. **Verify** approved count is 0:

```bash
curl "https://taxova.pro/api/admin/extraction-edit-stats?days=30" \
  -H "X-API-Key: $DASHBOARD_API_KEY"
```

Expect `approved_invoices: 0`. Local: use `http://127.0.0.1:8001` instead of taxova.pro.

3. Tick **Day-zero stats reset** on the dashboard pilot checklist.

---

## Firm + clients

1. Create warm firm + first CA (admin):

```bash
curl -X POST "https://taxova.pro/api/admin/firms" \
  -H "X-API-Key: $DASHBOARD_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"firm_name\":\"Pilot CA Firm\",\"ca_name\":\"Pilot CA\",\"invite_code\":\"654321\",\"password\":\"ChangeMeNow!\"}"
```

2. Open `/ca-assign` — link client WhatsApp numbers to that invite.
3. CA login on `/dashboard`: Settings → **1** → invite + password (not the shared API key for daily use).

---

## Train the CA (10 minutes)

- **Review next** = open next pending bill (easy ones first) → approve.
- Fix only filing-critical fields (GSTIN, totals, invoice #/date) when wrong.
- Portal offline ≠ Taxova broken → **Import GSTR-2B** + manual GSTIN.
- Sign out via Settings → **3** (do not share API key with the firm).

---

## During the pilot

| Approvals | Action |
|-----------|--------|
| 0–9 | Collect. Use **Review next**. Ignore edit-rate. |
| **~10** | Message CA: *“How’s this feeling so far?”* — listen only. No scale/hold. |
| 11–24 | Keep collecting. Glance at filing-critical fields only if curious. |
| **≥25** | Read pilot status: **Go** or **Hold** per `PILOT_CRITERIA.md`. |

Bars (locked): overall edit-rate **&lt; 25%**; each filing-critical field **&lt; 15%**.

---

## Founder checklist (also on dashboard)

- [ ] Day-zero stats reset (`approved_invoices = 0`)
- [ ] Warm firm + invite created
- [ ] Clients linked via `/ca-assign`
- [ ] CA trained (login, Review next, Import 2B)
- [ ] Calendar: check-in at ~10 approvals
- [ ] Calendar: week-1 review at ≥25 approvals

Richer AIS / e-filing wait until **Go**.
