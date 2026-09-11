# GST Filing (Phase 1)

Taxova **prepares** GSTR drafts. Legal filing still happens on **gst.gov.in**.

## What CAs do in the dashboard

1. Pick client + return month  
2. Clear **Month close** blockers (queue + GSTR-2B)  
3. Open **GST Filing** (sidebar **GSTR Filing**)  
4. **Export GSTR-1** / **Export GSTR-3B** → JSON download  
5. File on the portal using the steps on the panel  
6. Optionally **Mark prepared / filed** + ARN note (saved in browser only)

## APIs used

- `GET /api/metrics?client_phone=&month=` — readiness / blockers  
- `GET /api/gstr/export?client_phone=&month=&type=GSTR1|GSTR3B` — draft JSON  

## Not in Phase 1

- Live submit / ARN fetch via GSP  
- Server-persisted filing status (localStorage only for now)  

## Later (Phase 2)

Push return data through a licensed GSP into the government system, still producing a portal ARN.
