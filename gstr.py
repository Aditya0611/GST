"""
gstr.py — GSTR JSON Compiler

Aggregates approved invoice and line item records from the database
to output structured GSTR-1 and GSTR-3B reports.
"""

from datetime import datetime
import logging
import db
from processor import validate_gstin

logger = logging.getLogger("gstr")


def format_date_to_dd_mm_yyyy(date_str: str) -> str:
    """Converts YYYY-MM-DD to DD-MM-YYYY required by GSTR portal."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%d-%m-%Y")
    except Exception:
        # Fallback if parsing fails
        return date_str


async def generate_gstr1_json(client_phone: str, year_month: str) -> dict:
    """
    Compiles GSTR-1 return data (Outward supplies / Sales).
    Structures data for B2B (registered sales), B2CS (unregistered small sales), and HSN summaries.
    """
    client = await db.get_or_create_client(client_phone)
    client_gstin = client.get("gstin", "")

    # Fetch all approved invoices for the month
    all_invoices = await db.get_invoices(client_phone=client_phone, status="approved", month=year_month)

    # We filter only outward sales (where supplier_gstin matches client's GSTIN)
    sales_invoices = []
    for inv in all_invoices:
        # If client's GSTIN is set and matches the supplier, or if recipient's GSTIN matches client's and is a purchase, skip it
        # If client's GSTIN is not set, we treat client as the recipient (expenses only)
        if client_gstin and inv.get("supplier_gstin") == client_gstin:
            sales_invoices.append(inv)

    b2b_groups = {}  # Recipient GSTIN -> list of invoices
    b2cs_list = []   # List of aggregated B2CS records
    hsn_items = {}   # HSN Code -> aggregate details

    for inv in sales_invoices:
        inv_detail = await db.get_invoice_detail(inv["id"])
        if not inv_detail:
            continue

        rec_gstin = inv_detail.get("recipient_gstin")
        
        # 1. Classify B2B vs B2CS
        if rec_gstin and validate_gstin(rec_gstin):
            # Registered Buyer (B2B)
            if rec_gstin not in b2b_groups:
                b2b_groups[rec_gstin] = []
            
            # Map items
            items_list = []
            for idx, item in enumerate(inv_detail["line_items"]):
                items_list.append({
                    "num": idx + 1,
                    "itm_det": {
                        "txval": round(item["taxable_value"], 2),
                        "rt": round(item["gst_rate"], 2),
                        "iamt": round(item["igst"] or 0.0, 2),
                        "camt": round(item["cgst"] or 0.0, 2),
                        "samt": round(item["sgst"] or 0.0, 2)
                    }
                })

            b2b_groups[rec_gstin].append({
                "inum": inv_detail["invoice_number"],
                "idt": format_date_to_dd_mm_yyyy(inv_detail["invoice_date"]),
                "val": round(inv_detail["grand_total"], 2),
                "pos": inv_detail["place_of_supply"] or client_gstin[:2],
                "rchrg": "N",
                "inv_typ": "R",
                "itms": items_list
            })
        else:
            # Unregistered Buyer (B2CS - small B2C sales)
            # Grouped by Rate, POS (Place of Supply) and Supply Type
            pos = inv_detail["place_of_supply"] or client_gstin[:2] if client_gstin else "27"
            supply_type = inv_detail["supply_type"] or "INTRA-STATE"
            
            for item in inv_detail["line_items"]:
                # Check if matches existing group in b2cs_list
                found = False
                for b_item in b2cs_list:
                    if b_item["pos"] == pos and b_item["rt"] == item["gst_rate"] and b_item["sply_ty"] == supply_type:
                        b_item["txval"] += item["taxable_value"]
                        b_item["iamt"] += item["igst"] or 0.0
                        b_item["camt"] += item["cgst"] or 0.0
                        b_item["samt"] += item["sgst"] or 0.0
                        found = True
                        break
                
                if not found:
                    b2cs_list.append({
                        "sply_ty": supply_type,
                        "pos": pos,
                        "rt": round(item["gst_rate"], 2),
                        "txval": item["taxable_value"],
                        "iamt": item["igst"] or 0.0,
                        "camt": item["cgst"] or 0.0,
                        "samt": item["sgst"] or 0.0
                    })

        # 2. Compile HSN Summary
        for item in inv_detail["line_items"]:
            hsn = item["hsn_or_sac"] or "9999"  # Default fallback code
            key = f"{hsn}_{item['gst_rate']}"
            if key not in hsn_items:
                hsn_items[key] = {
                    "hsn_sc": hsn,
                    "desc": item["description"][:30] if item["description"] else "Services/Goods",
                    "uqc": "OTH",
                    "qty": 0.0,
                    "val": 0.0,
                    "txval": 0.0,
                    "iamt": 0.0,
                    "camt": 0.0,
                    "samt": 0.0
                }
            
            hsn_group = hsn_items[key]
            hsn_group["qty"] += item["quantity"] or 1.0
            hsn_group["val"] += item["line_total"]
            hsn_group["txval"] += item["taxable_value"]
            hsn_group["iamt"] += item["igst"] or 0.0
            hsn_group["camt"] += item["cgst"] or 0.0
            hsn_group["samt"] += item["sgst"] or 0.0

    # Format B2B output structure
    b2b_output = []
    for gstin, invoices in b2b_groups.items():
        b2b_output.append({
            "ctin": gstin,
            "inv": invoices
        })

    # Round all values in B2CS
    for b_item in b2cs_list:
        b_item["txval"] = round(b_item["txval"], 2)
        b_item["iamt"] = round(b_item["iamt"], 2)
        b_item["camt"] = round(b_item["camt"], 2)
        b_item["samt"] = round(b_item["samt"], 2)

    # Format HSN output
    hsn_output = []
    for idx, h_item in enumerate(hsn_items.values()):
        h_item["num"] = idx + 1
        h_item["qty"] = round(h_item["qty"], 2)
        h_item["val"] = round(h_item["val"], 2)
        h_item["txval"] = round(h_item["txval"], 2)
        h_item["iamt"] = round(h_item["iamt"], 2)
        h_item["camt"] = round(h_item["camt"], 2)
        h_item["samt"] = round(h_item["samt"], 2)
        hsn_output.append(h_item)

    # Complete GSTR-1 Structure
    return {
        "gstin": client_gstin,
        "fp": year_month.replace("-", ""),
        "b2b": b2b_output,
        "b2cs": b2cs_list,
        "hsn": {
            "data": hsn_output
        }
    }


async def generate_gstr3b_json(client_phone: str, year_month: str) -> dict:
    """
    Compiles GSTR-3B return summary (Tax liability and Eligible ITC).
    """
    from itc_rules import normalize_gstin, validate_gstin

    client = await db.get_or_create_client(client_phone)
    client_gstin = client.get("gstin", "")
    gstin = normalize_gstin(client_gstin)
    has_gstin = validate_gstin(gstin)

    # Fetch all approved invoices for the month
    all_invoices = await db.get_invoices(client_phone=client_phone, status="approved", month=year_month)

    # 1. Calculate Outward Taxable Supplies (Sales)
    osup_det = {"txval": 0.0, "iamt": 0.0, "camt": 0.0, "samt": 0.0}
    
    # 2. Calculate Eligible Input Tax Credit (ITC) from purchases
    # All Other ITC (itc_avl) vs Blocked ITC (itc_inegl)
    itc_avl_all = {"iamt": 0.0, "camt": 0.0, "samt": 0.0}
    itc_inegl_blocked = {"iamt": 0.0, "camt": 0.0, "samt": 0.0}

    for inv in all_invoices:
        if not has_gstin:
            continue

        inv_sup = normalize_gstin(inv.get("supplier_gstin"))
        inv_rec = normalize_gstin(inv.get("recipient_gstin"))
        is_sale = inv_sup == gstin
        is_purchase = inv_rec == gstin

        if is_sale:
            osup_det["txval"] += inv["total_taxable_value"]
            osup_det["iamt"] += inv["total_igst"] or 0.0
            osup_det["camt"] += inv["total_cgst"] or 0.0
            osup_det["samt"] += inv["total_sgst"] or 0.0
        elif is_purchase:
            # Prefer line-level rollups only when evaluation was run
            if inv.get("itc_line_evaluated"):
                itc_avl_all["camt"] += float(inv.get("itc_eligible_cgst") or 0)
                itc_avl_all["samt"] += float(inv.get("itc_eligible_sgst") or 0)
                itc_avl_all["iamt"] += float(inv.get("itc_eligible_igst") or 0)
                blocked = float(inv.get("itc_blocked_gst") or 0)
                # Split blocked proportionally across components if header taxes exist
                hdr = (inv.get("total_cgst") or 0) + (inv.get("total_sgst") or 0) + (inv.get("total_igst") or 0)
                if blocked and hdr:
                    itc_inegl_blocked["camt"] += blocked * ((inv.get("total_cgst") or 0) / hdr)
                    itc_inegl_blocked["samt"] += blocked * ((inv.get("total_sgst") or 0) / hdr)
                    itc_inegl_blocked["iamt"] += blocked * ((inv.get("total_igst") or 0) / hdr)
                elif blocked:
                    itc_inegl_blocked["camt"] += blocked / 2
                    itc_inegl_blocked["samt"] += blocked / 2
            elif inv.get("is_itc_eligible"):
                itc_avl_all["iamt"] += inv["total_igst"] or 0.0
                itc_avl_all["camt"] += inv["total_cgst"] or 0.0
                itc_avl_all["samt"] += inv["total_sgst"] or 0.0
            else:
                itc_inegl_blocked["iamt"] += inv["total_igst"] or 0.0
                itc_inegl_blocked["camt"] += inv["total_cgst"] or 0.0
                itc_inegl_blocked["samt"] += inv["total_sgst"] or 0.0
        # else: unclassified (wrong/missing party GSTIN) — skip

    # Round all values
    for k in osup_det:
        osup_det[k] = round(osup_det[k], 2)
    for k in itc_avl_all:
        itc_avl_all[k] = round(itc_avl_all[k], 2)
    for k in itc_inegl_blocked:
        itc_inegl_blocked[k] = round(itc_inegl_blocked[k], 2)

    # Complete GSTR-3B Structure
    return {
        "gstin": client_gstin,
        "fp": year_month.replace("-", ""),
        "sup_details": {
            "osup_det": osup_det
        },
        "itc_elg": {
            "itc_avl": [
                {
                    "ty": "AllOtherITC",
                    "iamt": itc_avl_all["iamt"],
                    "camt": itc_avl_all["camt"],
                    "samt": itc_avl_all["samt"]
                }
            ],
            "itc_inegl": [
                {
                    "ty": "BlockedITC_17_5",
                    "iamt": itc_inegl_blocked["iamt"],
                    "camt": itc_inegl_blocked["camt"],
                    "samt": itc_inegl_blocked["samt"]
                }
            ]
        }
    }
