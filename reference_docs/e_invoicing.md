# E-Invoicing Requirements Under GST

## What is E-Invoicing?

E-Invoicing (Electronic Invoicing) under GST is a system where B2B invoices are authenticated electronically by the **Invoice Registration Portal (IRP)** operated by the GST Network (GSTN). Once authenticated, each invoice gets a unique **Invoice Reference Number (IRN)** and a **QR code**.

E-Invoicing does **not** mean generating the invoice on a government portal. Businesses continue to generate invoices in their own software, but must upload them to the IRP for authentication.

## Applicability — Turnover Threshold

E-Invoicing is **mandatory** for registered taxpayers whose **aggregate annual turnover** (in any preceding financial year from 2017-18 onwards) exceeds the notified threshold:

| Notification | Threshold | Applicable From |
|---|---|---|
| Phase 1 | ₹500 crore | 1st October 2020 |
| Phase 2 | ₹100 crore | 1st January 2021 |
| Phase 3 | ₹50 crore | 1st April 2021 |
| Phase 4 | ₹20 crore | 1st April 2022 |
| Phase 5 | ₹10 crore | 1st October 2022 |
| Phase 6 | **₹5 crore** | **1st August 2023** |

> As of August 2023, **all businesses with turnover above ₹5 crore** must generate e-invoices for B2B transactions.

## Who is Exempt from E-Invoicing?

The following are **exempt** from e-invoicing regardless of turnover:
*   Insurance companies, banking companies, financial institutions, NBFCs
*   Goods Transport Agencies (GTA)
*   Passenger transport service providers
*   Multiplexes (cinema theatres)
*   Special Economic Zone (SEZ) units *(exporters are NOT exempt)*
*   Government departments and local authorities

## Which Documents Require E-Invoicing?

E-Invoicing is mandatory for:
*   **B2B Invoices** — issued to GST-registered buyers
*   **Exports** (with or without payment of IGST)
*   **Supplies to SEZ** (with or without tax)
*   **Debit Notes** and **Credit Notes** related to the above
*   **Deemed exports**

E-Invoicing is **NOT required** for:
*   **B2C Invoices** — issued to end consumers (unregistered persons)
*   **Nil-rated or exempt supplies**
*   **Delivery challans**
*   **Bill of supply** (for exempt goods/services by composition dealers)

## E-Invoicing Process

1.  **Generate invoice** in your billing/ERP software in the prescribed JSON format.
2.  **Upload to IRP** (Invoice Registration Portal) via API, mobile app, or GSP (GST Suvidha Provider).
3.  **IRP validates** the JSON, deduplicates, and generates:
    *   **IRN** (Invoice Reference Number) — 64-character hash
    *   **Signed e-invoice JSON**
    *   **QR code** containing key invoice details
4.  **Print the QR code** on the physical invoice before sending to the buyer.
5.  IRP auto-populates **GSTR-1** with e-invoice data — no manual entry needed.

## Key Fields in E-Invoice

*   Supplier GSTIN, Legal Name
*   Buyer GSTIN, Legal Name, billing/shipping address
*   Invoice number, date, type (Regular/Export/SEZ/Deemed Export)
*   HSN code (mandatory, 6-digit for turnover > ₹5 crore)
*   Item description, quantity, unit, unit price
*   Taxable value, discount, GST rates (IGST/CGST/SGST), total tax, total amount
*   Place of Supply (state code)
*   Reverse charge applicable (Yes/No)

## IRN — Invoice Reference Number

*   Each IRN is unique and valid **only once** — cannot be regenerated for the same invoice.
*   An IRN is valid for **30 days** for the purpose of cancellation.
*   After 30 days, an e-invoice **cannot be cancelled** on the IRP (though amendments can be made in GSTR-1).

## Penalties for Non-Compliance

*   Not generating an e-invoice when required: penalty of **₹10,000 per invoice** under Section 122 of CGST Act.
*   Buyer cannot claim ITC on an invoice that was required to have an IRN but does not have one.
*   QR code not printed on invoice: equivalent to not issuing a valid tax invoice.

## Cancelled E-Invoices

*   E-invoice can be cancelled on IRP within **24 hours** of generation (some portals allow up to 30 days).
*   After cancellation, a new invoice with a new invoice number must be issued and registered.
*   Partially cancelling an e-invoice is not allowed — full cancellation only.

## Auto-Population to GSTR-1

*   Authenticated e-invoice data flows automatically into **GSTR-1** Tables 4A, 4B, 4C, 6B, 6C.
*   Taxpayers should verify auto-populated data before filing GSTR-1.

## IRP Portals (Invoice Registration Portals)

Multiple IRPs are authorised by GSTN:
*   NIC IRP: `einvoice1.gst.gov.in`
*   Cleartax IRP, IRIS IRP, and others (private GSPs)
