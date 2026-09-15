"""
agent.py — Agentic Taxova.ai assistant.

A multi-step agent (Gemini or Groq) that can call domain tools (rules search,
ITC evaluation, invoice lookup, monthly metrics) instead of answering from a
single RAG prompt alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any, Optional

from dotenv import load_dotenv
from google import genai
from google.genai import types
from groq import Groq

import db
import rag
from processor import evaluate_itc_eligibility, validate_gstin

load_dotenv(override=True)

logger = logging.getLogger("agent")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

AGENT_LLM_PROVIDER = os.getenv("AGENT_LLM_PROVIDER", "groq").lower().strip()
MAX_TOOL_ROUNDS = int(os.getenv("AGENT_MAX_TOOL_ROUNDS", "6"))

if AGENT_LLM_PROVIDER == "groq":
    AGENT_MODEL = os.getenv("AGENT_MODEL", "qwen/qwen3.6-27b")
else:
    AGENT_MODEL = os.getenv("AGENT_MODEL", "models/gemini-2.5-flash")


SYSTEM_INSTRUCTION = """You are the taxova.pro Agent for Indian GST compliance.

You help clients and CAs with:
- Input Tax Credit (ITC) eligibility and amounts on specific invoices
- Monthly GST summaries (sales liability, eligible ITC, net payable)
- Filing readiness (pending reviews, GSTR-1 / GSTR-3B status)
- GST rules (Section 16, Section 17(5) blocked credits, RCM, place of supply)

Rules:
1. Prefer calling tools over guessing about this client's invoices or metrics.
2. For ITC on a bill: use get_invoice_itc when an invoice id is known; otherwise list invoices first.
3. Cite tool results clearly. If a tool returns blocked ITC, explain the reason briefly.
4. Keep WhatsApp-style answers concise; dashboard answers can be slightly more detailed.
5. Never invent GSTIN validation or invoice totals — use tools.
6. If the user asks a pure law question, use search_gst_rules and prefer higher-score matches; cite the document/section title. If matches are weak, say the knowledge base may not cover it — do not invent sections.
7. If client context is missing and the question needs it, say what phone/client is required.
"""

# OpenAI-compatible tool schemas (used by Groq; Gemini tools built separately)
GROQ_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "search_gst_rules",
            "description": "Search the GST knowledge base (ITC, blocked credits, filing, RCM, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language GST compliance question.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max chunks to return (1-8). Default 5.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_gstin_format",
            "description": "Validate whether a string matches Indian GSTIN format.",
            "parameters": {
                "type": "object",
                "properties": {"gstin": {"type": "string"}},
                "required": ["gstin"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_monthly_summary",
            "description": "Get sales, expenses, GST liability, eligible ITC, and pending reviews for a month.",
            "parameters": {
                "type": "object",
                "properties": {
                    "year_month": {
                        "type": "string",
                        "description": "YYYY-MM. Defaults to current month.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_filing_status",
            "description": "Summarize filing readiness for GSTR-1 / GSTR-3B for a month.",
            "parameters": {
                "type": "object",
                "properties": {
                    "year_month": {
                        "type": "string",
                        "description": "YYYY-MM. Defaults to current month.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_invoices",
            "description": "List recent invoices for the active client (optionally filter by status).",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "One of: all, pending, approved, flagged.",
                    },
                    "month": {
                        "type": "string",
                        "description": "Optional YYYY-MM filter.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max rows as an integer 1-20. Default 8. Always pass a number, not a string.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_invoice_itc",
            "description": (
                "Get one invoice's GST breakdown and claimable ITC amount. "
                "ITC amount = CGST+SGST+IGST when eligible, else 0."
            ),
            "parameters": {
                "type": "object",
                "properties": {"invoice_id": {"type": "integer"}},
                "required": ["invoice_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evaluate_itc_for_expense",
            "description": (
                "Decide ITC eligibility for an expense category / line-item description "
                "under Section 17(5) using the GST knowledge base."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Business category, e.g. Food & Beverages, Software & SaaS.",
                    },
                    "line_items_summary": {
                        "type": "string",
                        "description": "Short description of items on the bill.",
                    },
                    "recipient_gstin": {
                        "type": "string",
                        "description": "Buyer GSTIN if known. Empty means unregistered.",
                    },
                },
                "required": ["category"],
            },
        },
    },
]


def _tool_declarations() -> list[types.Tool]:
    return [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name="search_gst_rules",
                    description="Search the GST knowledge base (ITC, blocked credits, filing, RCM, etc.).",
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "query": types.Schema(
                                type=types.Type.STRING,
                                description="Natural language GST compliance question.",
                            ),
                            "limit": types.Schema(
                                type=types.Type.INTEGER,
                                description="Max chunks to return (1-8). Default 5.",
                            ),
                        },
                        required=["query"],
                    ),
                ),
                types.FunctionDeclaration(
                    name="validate_gstin_format",
                    description="Validate whether a string matches Indian GSTIN format.",
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "gstin": types.Schema(type=types.Type.STRING),
                        },
                        required=["gstin"],
                    ),
                ),
                types.FunctionDeclaration(
                    name="get_monthly_summary",
                    description="Get sales, expenses, GST liability, eligible ITC, and pending reviews for a month.",
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "year_month": types.Schema(
                                type=types.Type.STRING,
                                description="YYYY-MM. Defaults to current month.",
                            ),
                        },
                    ),
                ),
                types.FunctionDeclaration(
                    name="get_filing_status",
                    description="Summarize filing readiness for GSTR-1 / GSTR-3B for a month.",
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "year_month": types.Schema(
                                type=types.Type.STRING,
                                description="YYYY-MM. Defaults to current month.",
                            ),
                        },
                    ),
                ),
                types.FunctionDeclaration(
                    name="list_invoices",
                    description="List recent invoices for the active client (optionally filter by status).",
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "status": types.Schema(
                                type=types.Type.STRING,
                                description="One of: all, pending, approved, flagged.",
                            ),
                            "month": types.Schema(
                                type=types.Type.STRING,
                                description="Optional YYYY-MM filter.",
                            ),
                            "limit": types.Schema(
                                type=types.Type.INTEGER,
                                description="Max rows (1-20). Default 8.",
                            ),
                        },
                    ),
                ),
                types.FunctionDeclaration(
                    name="get_invoice_itc",
                    description=(
                        "Get one invoice's GST breakdown and claimable ITC amount. "
                        "ITC amount = CGST+SGST+IGST when eligible, else 0."
                    ),
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "invoice_id": types.Schema(type=types.Type.INTEGER),
                        },
                        required=["invoice_id"],
                    ),
                ),
                types.FunctionDeclaration(
                    name="evaluate_itc_for_expense",
                    description=(
                        "Decide ITC eligibility for an expense category / line-item description "
                        "under Section 17(5) using the GST knowledge base."
                    ),
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "category": types.Schema(
                                type=types.Type.STRING,
                                description="Business category, e.g. Food & Beverages, Software & SaaS.",
                            ),
                            "line_items_summary": types.Schema(
                                type=types.Type.STRING,
                                description="Short description of items on the bill.",
                            ),
                            "recipient_gstin": types.Schema(
                                type=types.Type.STRING,
                                description="Buyer GSTIN if known. Empty means unregistered.",
                            ),
                        },
                        required=["category"],
                    ),
                ),
            ]
        )
    ]


class AgentContext:
    def __init__(self, client_phone: Optional[str] = None, channel: str = "dashboard"):
        self.client_phone = (client_phone or "").strip() or None
        self.channel = channel  # dashboard | whatsapp


async def _exec_tool(name: str, args: dict, ctx: AgentContext) -> Any:
    args = args or {}

    if name == "search_gst_rules":
        query = str(args.get("query", "")).strip()
        limit = max(1, min(int(args.get("limit") or 5), 8))
        matches = await rag.search_knowledge_base(query, limit=limit)
        return {
            "matches": [
                {
                    "title": m.get("title"),
                    "section": m.get("section") or "",
                    "similarity": round(float(m.get("similarity", 0)), 3),
                    "score": round(float(m.get("score", m.get("similarity", 0))), 3),
                    "content": (m.get("content") or "")[:900],
                }
                for m in matches
            ],
            "note": (
                "Prefer higher score matches. Cite section titles when answering. "
                "If matches are weak or empty, say the knowledge base did not cover it."
            ),
        }

    if name == "validate_gstin_format":
        gstin = str(args.get("gstin", "")).strip().upper()
        return {"gstin": gstin, "is_valid": validate_gstin(gstin)}

    if name == "get_monthly_summary":
        if not ctx.client_phone:
            return {"error": "No client_phone in context. Ask the user which client / WhatsApp number."}
        year_month = str(args.get("year_month") or datetime.now().strftime("%Y-%m"))
        metrics = await db.get_monthly_metrics(ctx.client_phone, year_month)
        return {"year_month": year_month, "client_phone": ctx.client_phone, **metrics}

    if name == "get_filing_status":
        if not ctx.client_phone:
            return {"error": "No client_phone in context."}
        year_month = str(args.get("year_month") or datetime.now().strftime("%Y-%m"))
        metrics = await db.get_monthly_metrics(ctx.client_phone, year_month)
        pending = metrics.get("pending_review", 0)
        return {
            "year_month": year_month,
            "gstr1": "In preparation" if pending else "Ready to compile",
            "gstr3b": "Open (due next month)",
            "pending_ca_review": pending,
            "eligible_itc": metrics.get("itc_claimed", 0),
            "sales_gst_liability": metrics.get("sales_gst_liability", 0),
            "net_gst_payable": metrics.get("net_gst_payable", 0),
        }

    if name == "list_invoices":
        if not ctx.client_phone:
            return {"error": "No client_phone in context."}
        status_raw = str(args.get("status") or "all").lower()
        month = args.get("month") or None
        try:
            limit = max(1, min(int(args.get("limit") or 8), 20))
        except (TypeError, ValueError):
            limit = 8
        status = None
        if status_raw == "pending":
            status = "pending"
        elif status_raw == "approved":
            status = "approved"
        invoices = await db.get_invoices(ctx.client_phone, status=status, month=month)
        if status_raw == "flagged":
            invoices = [i for i in invoices if not i.get("is_calculation_correct")]
        slim = []
        for inv in invoices[:limit]:
            gst = (inv.get("total_cgst") or 0) + (inv.get("total_sgst") or 0) + (inv.get("total_igst") or 0)
            slim.append(
                {
                    "id": inv.get("id"),
                    "invoice_number": inv.get("invoice_number"),
                    "supplier_name": inv.get("supplier_name"),
                    "invoice_date": inv.get("invoice_date"),
                    "category": inv.get("business_category"),
                    "grand_total": inv.get("grand_total"),
                    "gst_total": round(float(gst), 2),
                    "is_itc_eligible": bool(inv.get("is_itc_eligible")),
                    "is_approved": bool(inv.get("is_approved")),
                    "is_calculation_correct": bool(inv.get("is_calculation_correct")),
                }
            )
        return {"count": len(slim), "invoices": slim}

    if name == "get_invoice_itc":
        invoice_id = int(args.get("invoice_id"))
        detail = await db.get_invoice_detail(invoice_id)
        if not detail:
            return {"error": f"Invoice {invoice_id} not found."}
        if ctx.client_phone and detail.get("client_phone") and detail["client_phone"] != ctx.client_phone:
            return {"error": "Invoice does not belong to the active client."}

        cgst = float(detail.get("total_cgst") or 0)
        sgst = float(detail.get("total_sgst") or 0)
        igst = float(detail.get("total_igst") or 0)
        gst_total = round(cgst + sgst + igst, 2)
        eligible = bool(detail.get("is_itc_eligible"))
        claimable = gst_total if eligible else 0.0
        return {
            "invoice_id": invoice_id,
            "invoice_number": detail.get("invoice_number"),
            "supplier_name": detail.get("supplier_name"),
            "category": detail.get("business_category"),
            "taxable_value": detail.get("total_taxable_value"),
            "cgst": cgst,
            "sgst": sgst,
            "igst": igst,
            "gst_total": gst_total,
            "is_itc_eligible": eligible,
            "itc_ineligibility_reason": detail.get("itc_ineligibility_reason"),
            "claimable_itc": claimable,
            "is_approved": bool(detail.get("is_approved")),
            "formula": "claimable_itc = (CGST+SGST+IGST) if eligible else 0",
        }

    if name == "evaluate_itc_for_expense":
        category = str(args.get("category", "")).strip()
        summary = str(args.get("line_items_summary") or "")
        recipient_gstin = str(args.get("recipient_gstin") or "").strip().upper()
        # If GSTIN omitted in exploratory Qs, assume registered buyer so Sec 17(5) rules apply
        is_registered = validate_gstin(recipient_gstin) if recipient_gstin else True
        eligible, reason = await evaluate_itc_eligibility(category, is_registered, summary)
        return {
            "category": category,
            "is_itc_eligible": eligible,
            "reason": reason,
            "assumed_registered_recipient": not bool(recipient_gstin),
        }

    return {"error": f"Unknown tool: {name}"}


def _extract_function_calls(response) -> list[tuple[str, dict, str]]:
    """Return list of (name, args, call_id)."""
    calls = []
    if not response or not getattr(response, "candidates", None):
        return calls
    cand = response.candidates[0]
    content = getattr(cand, "content", None)
    if not content or not getattr(content, "parts", None):
        return calls
    for part in content.parts:
        fc = getattr(part, "function_call", None)
        if not fc:
            continue
        name = fc.name
        raw_args = dict(fc.args) if fc.args else {}
        call_id = getattr(fc, "id", None) or name
        calls.append((name, raw_args, call_id))
    return calls


def _model_text(response) -> str:
    try:
        text = (response.text or "").strip()
        if text:
            return text
    except Exception:
        pass
    return ""


async def run_gst_agent(
    message: str,
    client_phone: Optional[str] = None,
    channel: str = "dashboard",
) -> dict:
    """
    Run the agentic loop. Returns:
      { "response": str, "steps": [ {tool, args, result} ], "model": str }
    """
    provider = AGENT_LLM_PROVIDER
    if provider == "groq" and not groq_client:
        if client:
            logger.warning("AGENT_LLM_PROVIDER=groq but GROQ_API_KEY missing — falling back to Gemini")
            provider = "gemini"
        else:
            return {
                "response": "Agent is not configured. Add GROQ_API_KEY (or GEMINI_API_KEY) to your .env file.",
                "steps": [],
                "model": AGENT_MODEL,
            }
    if provider != "groq" and not client:
        if groq_client:
            logger.warning("Gemini unavailable — falling back to Groq for agent")
            provider = "groq"
        else:
            return {
                "response": "Agent is not configured. Add GROQ_API_KEY or GEMINI_API_KEY to your .env file.",
                "steps": [],
                "model": AGENT_MODEL,
            }

    ctx = AgentContext(client_phone=client_phone, channel=channel)
    try:
        if provider == "groq":
            try:
                return await _run_groq_agent(message, ctx)
            except Exception as groq_err:
                err = str(groq_err)
                model_gone = (
                    "model_not_found" in err
                    or "does not exist" in err.lower()
                    or "404" in err
                )
                if model_gone and client:
                    logger.warning(
                        "Groq model unavailable (%s) — falling back to Gemini",
                        err[:180],
                    )
                    return await _run_gemini_agent(message, ctx)
                raise
    except Exception as e:
        logger.exception("Agent run failed:")
        try:
            fallback = await rag.answer_query(message)
            return {
                "response": fallback,
                "steps": [{"tool": "fallback_rag", "args": {}, "result": {"ok": True}}],
                "model": AGENT_MODEL,
                "warning": str(e),
            }
        except Exception:
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err or "quota" in err.lower():
                msg = (
                    "I'm temporarily at AI capacity. "
                    "Please wait a minute and try again."
                )
            else:
                msg = "Sorry, I couldn't process that just now. Please try again shortly."
            return {
                "response": msg,
                "steps": [],
                "model": AGENT_MODEL,
            }


async def _run_groq_agent(message: str, ctx: AgentContext) -> dict:
    steps: list[dict] = []
    model = os.getenv("AGENT_MODEL", "qwen/qwen3.6-27b")
    user_preamble = (
        f"Channel: {ctx.channel}\n"
        f"Active client_phone: {ctx.client_phone or '(none)'}\n\n"
        f"User message:\n{message}"
    )
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_INSTRUCTION},
        {"role": "user", "content": user_preamble},
    ]

    for round_idx in range(MAX_TOOL_ROUNDS):
        logger.info(
            "Agent round %d (groq) for channel=%s phone=%s",
            round_idx + 1,
            ctx.channel,
            ctx.client_phone,
        )

        def _call():
            return groq_client.chat.completions.create(
                model=model,
                messages=messages,
                tools=GROQ_TOOLS,
                tool_choice="auto",
                temperature=0.2,
            )

        response = await asyncio.to_thread(_call)
        choice = response.choices[0].message
        tool_calls = getattr(choice, "tool_calls", None) or []

        if not tool_calls:
            answer = (choice.content or "").strip() or (
                "I could not produce an answer. Please try rephrasing."
            )
            return {"response": answer, "steps": steps, "model": model, "provider": "groq"}

        messages.append(
            {
                "role": "assistant",
                "content": choice.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )

        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            logger.info("Agent tool call: %s(%s)", name, args)
            try:
                result = await _exec_tool(name, args, ctx)
            except Exception as e:
                logger.exception("Tool %s failed:", name)
                result = {"error": str(e)}
            steps.append({"tool": name, "args": args, "result": result})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, default=str),
                }
            )

    messages.append(
        {
            "role": "user",
            "content": "Please answer now using the tool results you already have.",
        }
    )

    def _final():
        return groq_client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.2,
        )

    final = await asyncio.to_thread(_final)
    answer = (final.choices[0].message.content or "").strip() or (
        "I gathered data but ran out of tool rounds. Please ask a more specific question."
    )
    return {"response": answer, "steps": steps, "model": model, "provider": "groq"}


async def _run_gemini_agent(message: str, ctx: AgentContext) -> dict:
    steps: list[dict] = []
    gemini_model = os.getenv("GEMINI_AGENT_MODEL", "models/gemini-2.5-flash")
    if not gemini_model.startswith("models/"):
        gemini_model = f"models/{gemini_model}"
    user_preamble = (
        f"Channel: {ctx.channel}\n"
        f"Active client_phone: {ctx.client_phone or '(none)'}\n\n"
        f"User message:\n{message}"
    )

    contents: list[types.Content] = [
        types.Content(role="user", parts=[types.Part.from_text(text=user_preamble)])
    ]

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        tools=_tool_declarations(),
        temperature=0.2,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    for round_idx in range(MAX_TOOL_ROUNDS):
        logger.info(
            "Agent round %d (gemini) for channel=%s phone=%s",
            round_idx + 1,
            ctx.channel,
            ctx.client_phone,
        )
        response = await client.aio.models.generate_content(
            model=gemini_model,
            contents=contents,
            config=config,
        )

        calls = _extract_function_calls(response)
        if not calls:
            answer = _model_text(response) or (
                "I could not produce an answer. Please try rephrasing."
            )
            return {
                "response": answer,
                "steps": steps,
                "model": gemini_model,
                "provider": "gemini",
            }

        model_content = response.candidates[0].content
        contents.append(model_content)

        fn_response_parts = []
        for name, args, _call_id in calls:
            logger.info("Agent tool call: %s(%s)", name, args)
            try:
                result = await _exec_tool(name, args, ctx)
            except Exception as e:
                logger.exception("Tool %s failed:", name)
                result = {"error": str(e)}

            steps.append({"tool": name, "args": args, "result": result})
            fn_response_parts.append(
                types.Part.from_function_response(
                    name=name,
                    response={"result": result},
                )
            )

        contents.append(types.Content(role="user", parts=fn_response_parts))

    contents.append(
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(
                    text="Please answer now using the tool results you already have."
                )
            ],
        )
    )
    final = await client.aio.models.generate_content(
        model=gemini_model,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0.2,
        ),
    )
    answer = _model_text(final) or (
        "I gathered data but ran out of tool rounds. Please ask a more specific question."
    )
    return {"response": answer, "steps": steps, "model": gemini_model, "provider": "gemini"}
