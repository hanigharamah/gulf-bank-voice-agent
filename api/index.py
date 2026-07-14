"""
Mock Gulf-bank core-banking backend — Supabase-backed, Vercel-deployed variant.

This is the same webhook contract and security design as the original
Flask + local-JSON backend, with the storage layer swapped to Supabase
Postgres so it works from Vercel's stateless serverless functions:

    original (in-memory / JSON file)         ->  this version (Supabase)
    ---------------------------------------------------------------------
    DB["accounts"][id]                       ->  accounts / transactions tables
    verified_sessions dict (process memory)  ->  verified_sessions table,
                                                  TTL becomes a WHERE filter
    DB["processed_tool_calls"] + save_db()   ->  processed_tool_calls table
    push_event() -> SSE /events stream       ->  tool_call_events table,
                                                  read via Supabase Realtime
                                                  (postgres_changes) by the
                                                  frontend directly — this
                                                  backend no longer serves
                                                  /events at all.

Everything else — the webhook envelope parsing, the security gating (bind
verification to conversation_id, never trust the LLM), idempotency-by-
tool-call-id, halalas-only money — is unchanged from the original design.
"""

import hmac
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Optional

from flask import Flask, jsonify, request
from flask_cors import CORS
from supabase import create_client

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

VERIFICATION_TTL_SECONDS = int(os.environ.get("VERIFICATION_TTL_SECONDS", "600"))


def _missing_config() -> list[str]:
    missing = []
    if not os.environ.get("WEBHOOK_SECRET"):
        missing.append("WEBHOOK_SECRET")
    if not os.environ.get("SUPABASE_URL"):
        missing.append("SUPABASE_URL")
    if not os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
        missing.append("SUPABASE_SERVICE_ROLE_KEY")
    return missing


def _config_error_response():
    missing = _missing_config()
    return jsonify({
        "result": {
            "ok": False,
            "error": "misconfigured",
            "message": (
                "The banking backend is not configured. Missing Vercel "
                f"environment variables: {', '.join(missing)}."
            ),
        }
    }), 503


_supabase = None


def get_supabase():
    global _supabase
    if _supabase is None:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not set.")
        _supabase = create_client(url, key)
    return _supabase

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("bank")

app = Flask(__name__)
CORS(app)

# Server-side client, authenticated with the service_role key — this is the
# only key that ever touches this backend, and it bypasses RLS entirely.
# The anon/public key is used exclusively client-side by the frontend to
# read the tool_call_events table (see schema_realtime.sql).
# Initialized lazily so import succeeds even when env vars are missing —
# Vercel would otherwise return FUNCTION_INVOCATION_FAILED before any
# route handler can run.


def format_halalas(halalas: int) -> str:
    sign = "-" if halalas < 0 else ""
    riyals, cents = divmod(abs(halalas), 100)
    return f"{sign}SAR {riyals:,}.{cents:02d}"


# --------------------------------------------------------------------------
# Webhook parsing — identical to the original: flat body is the documented
# ElevenLabs format, nested {tool_call_id, tool_name, parameters,
# conversation_id} envelope is a tolerated fallback.
# --------------------------------------------------------------------------

class CallContext:
    def __init__(self, params, conversation_id, idempotency_key, body_format):
        self.params = params
        self.conversation_id = conversation_id
        self.idempotency_key = idempotency_key
        self.body_format = body_format


def parse_webhook(body: dict) -> CallContext:
    if isinstance(body.get("parameters"), dict):
        log.warning("Envelope-style body received — falling back to nested 'parameters'.")
        params = body["parameters"]
        conversation_id = body.get("conversation_id") or params.get("conversation_id")
        tool_call_id = body.get("tool_call_id")
        fmt = "envelope"
    else:
        params = body
        conversation_id = params.get("conversation_id")
        tool_call_id = params.get("tool_call_id")
        fmt = "flat"

    idempotency_key = tool_call_id or params.get("idempotency_key")
    return CallContext(params, conversation_id, idempotency_key, fmt)


# --------------------------------------------------------------------------
# Verified sessions — the session-bound verification design, now backed by
# a table instead of a process-local dict so it works across stateless
# serverless invocations (a retry of the same conversation might land on a
# completely different function instance — the DB is the only shared state).
# --------------------------------------------------------------------------

def get_verified_account(conversation_id) -> Optional[str]:
    if not conversation_id:
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=VERIFICATION_TTL_SECONDS)).isoformat()
    resp = (
        get_supabase().table("verified_sessions")
        .select("account_id, verified_at")
        .eq("conversation_id", conversation_id)
        .gte("verified_at", cutoff)
        .limit(1)
        .execute()
    )
    if not resp.data:
        return None
    return resp.data[0]["account_id"]


def check_access(ctx: CallContext, account_id: str):
    verified_account = get_verified_account(ctx.conversation_id)
    if verified_account is None:
        return {
            "ok": False, "error": "not_verified",
            "message": ("I can't access account details yet — the customer's identity "
                        "has not been verified in this conversation. Please verify "
                        "their identity first."),
        }
    if verified_account != account_id:
        return {
            "ok": False, "error": "account_mismatch",
            "message": ("This conversation is verified for a different account. "
                        "Please verify the customer's identity for this account first."),
        }
    return None


def get_account(account_id):
    account_id = str(account_id or "")
    resp = get_supabase().table("accounts").select("*").eq("account_id", account_id).limit(1).execute()
    if not resp.data:
        return None, {
            "ok": False, "error": "unknown_account",
            "message": ("I couldn't find an account with that number. Please ask the "
                        "customer to repeat their account number."),
        }
    return resp.data[0], None


# --------------------------------------------------------------------------
# Live tool-call panel — Supabase Realtime replaces the original SSE stream.
# INSERT on "received", UPDATE on "completed"/"error" — the frontend's
# card-merge logic (keyed on call_id) already handles both event types
# identically, whether they arrive as a fresh row or a change to one.
# --------------------------------------------------------------------------

def emit_event(call_id: str, status: str, tool: str, params: dict, result=None):
    if status == "received":
        get_supabase().table("tool_call_events").insert({
            "call_id": call_id, "tool_name": tool, "status": status,
            "parameters": params, "result": None,
        }).execute()
    else:
        get_supabase().table("tool_call_events").update({
            "status": status, "result": result,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("call_id", call_id).execute()


# --------------------------------------------------------------------------
# webhook_tool decorator — same pipeline as the original: auth -> parse ->
# emit 'received' -> idempotency replay -> handler -> persist idempotency
# record -> emit 'completed'/'error'.
# --------------------------------------------------------------------------

def webhook_tool(tool_name: str, mutating: bool = False):
    def decorator(handler):
        @wraps(handler)
        def wrapper():
            if _missing_config():
                log.error("✗ %-28s rejected: backend not configured", tool_name)
                return _config_error_response()

            provided = request.headers.get("X-Webhook-Secret", "")
            webhook_secret = os.environ.get("WEBHOOK_SECRET", "")
            if not webhook_secret or not hmac.compare_digest(provided, webhook_secret):
                log.warning("✗ %-28s rejected: bad or missing X-Webhook-Secret", tool_name)
                return jsonify({"result": {
                    "ok": False, "error": "unauthorized",
                    "message": "Webhook authentication failed.",
                }}), 401

            body = request.get_json(silent=True)
            if not isinstance(body, dict):
                return jsonify({"result": {
                    "ok": False, "error": "malformed_request",
                    "message": "The request body must be a JSON object.",
                }}), 400
            ctx = parse_webhook(body)

            call_id = uuid.uuid4().hex[:8]
            log.info("→ %-28s in  [%s] %s", tool_name, ctx.body_format, ctx.params)
            emit_event(call_id, "received", tool_name, ctx.params)

            try:
                replayed = False
                if mutating and ctx.idempotency_key:
                    existing = (
                        get_supabase().table("processed_tool_calls")
                        .select("result")
                        .eq("idempotency_key", ctx.idempotency_key)
                        .limit(1)
                        .execute()
                    )
                    if existing.data:
                        result = dict(existing.data[0]["result"])
                        result["replayed"] = True
                        replayed = True
                        log.info("↺ %-28s duplicate key %r — replaying stored result",
                                 tool_name, ctx.idempotency_key)
                if not replayed:
                    if mutating and not ctx.idempotency_key:
                        log.warning("  %-28s mutating call without an idempotency key", tool_name)
                    result = handler(ctx)
                    if mutating and ctx.idempotency_key:
                        get_supabase().table("processed_tool_calls").upsert({
                            "idempotency_key": ctx.idempotency_key,
                            "tool_name": tool_name,
                            "result": result,
                        }).execute()
            except Exception:
                log.exception("✗ %-28s unhandled error", tool_name)
                error_result = {
                    "ok": False, "error": "internal_error",
                    "message": ("Something went wrong on the banking system. "
                                "Please apologise to the customer and try again."),
                }
                emit_event(call_id, "error", tool_name, ctx.params, error_result)
                return jsonify({"result": error_result}), 500

            status = "completed" if result.get("ok", True) else "error"
            symbol = "✓" if status == "completed" else "✗"
            log.info("%s %-28s out %s", symbol, tool_name, result)
            emit_event(call_id, status, tool_name, ctx.params, result)
            return jsonify({"result": result}), 200

        return wrapper
    return decorator


# --------------------------------------------------------------------------
# Tool endpoints — identical business logic to the original backend.
# --------------------------------------------------------------------------

@app.post("/tools/verify-identity")
@webhook_tool("verify_customer_identity")
def verify_identity(ctx: CallContext):
    account, error = get_account(ctx.params.get("account_id"))
    if error:
        return error

    provided = str(ctx.params.get("last_four_national_id", "")).strip()
    if provided != account["verification_last_four"]:
        return {
            "ok": False, "verified": False, "error": "verification_failed",
            "message": ("Verification failed — the details provided do not "
                        "match our records. Please ask the customer to try again."),
        }

    if not ctx.conversation_id:
        return {
            "ok": False, "verified": False, "error": "missing_conversation_id",
            "message": ("Verification could not be completed because no "
                        "conversation id was sent with this tool call."),
        }

    get_supabase().table("verified_sessions").upsert({
        "conversation_id": ctx.conversation_id,
        "account_id": account["account_id"],
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }).execute()

    return {
        "ok": True, "verified": True,
        "account_id": account["account_id"],
        "holder_name": account["holder_name"],
        "valid_for_minutes": VERIFICATION_TTL_SECONDS // 60,
        "message": (f"Identity verified for {account['holder_name']}. You may "
                    "now help them with account services in this conversation."),
    }


@app.post("/tools/get-balance")
@webhook_tool("get_account_balance")
def get_balance(ctx: CallContext):
    account, error = get_account(ctx.params.get("account_id"))
    if error:
        return error
    denied = check_access(ctx, account["account_id"])
    if denied:
        return denied

    balance = account["balance_halalas"]
    return {
        "ok": True,
        "account_id": account["account_id"],
        "balance_halalas": balance,
        "balance_display": format_halalas(balance),
        "card_status": account["card_status"],
        "message": f"The current balance is {format_halalas(balance)}.",
    }


@app.post("/tools/get-transactions")
@webhook_tool("get_recent_transactions")
def get_transactions(ctx: CallContext):
    account, error = get_account(ctx.params.get("account_id"))
    if error:
        return error
    denied = check_access(ctx, account["account_id"])
    if denied:
        return denied

    try:
        count = max(1, min(int(ctx.params.get("count", 5)), 20))
    except (TypeError, ValueError):
        count = 5

    resp = (
        get_supabase().table("transactions")
        .select("occurred_on, merchant, amount_halalas, country, flagged")
        .eq("account_id", account["account_id"])
        .order("occurred_on", desc=True)
        .limit(count)
        .execute()
    )

    transactions = []
    for tx in resp.data:
        transactions.append({
            "date": tx["occurred_on"],
            "merchant": tx["merchant"],
            "amount_halalas": tx["amount_halalas"],
            "amount_display": format_halalas(tx["amount_halalas"]),
            "country": tx["country"],
            "flagged": tx.get("flagged", False),
        })

    flagged = [tx for tx in transactions if tx["flagged"]]
    message = f"Here are the {len(transactions)} most recent transactions."
    if flagged:
        tx = flagged[0]
        message += (f" Note: the {tx['amount_display']} charge from "
                    f"{tx['merchant']} on {tx['date']} is flagged as unusual "
                    "for this account — you may want to ask the customer if "
                    "they recognise it, and offer to freeze the card if not.")
    return {
        "ok": True,
        "account_id": account["account_id"],
        "transactions": transactions,
        "flagged_count": len(flagged),
        "message": message,
    }


def _set_card_status(ctx: CallContext, target_status: str, verb: str):
    account, error = get_account(ctx.params.get("account_id"))
    if error:
        return error
    denied = check_access(ctx, account["account_id"])
    if denied:
        return denied

    if account["card_status"] == target_status:
        return {
            "ok": True, "card_status": target_status, "changed": False,
            "account_id": account["account_id"],
            "message": (f"The card for account {account['account_id']} is "
                        f"already {target_status} — no action was needed."),
        }

    get_supabase().table("accounts").update({"card_status": target_status}).eq(
        "account_id", account["account_id"]
    ).execute()

    return {
        "ok": True, "card_status": target_status, "changed": True,
        "account_id": account["account_id"],
        "message": (f"Done — the card for account {account['account_id']} "
                    f"has been {verb}."),
    }


@app.post("/tools/freeze-card")
@webhook_tool("freeze_customer_card", mutating=True)
def freeze_card(ctx: CallContext):
    return _set_card_status(ctx, "frozen", "frozen")


@app.post("/tools/unfreeze-card")
@webhook_tool("unfreeze_customer_card", mutating=True)
def unfreeze_card(ctx: CallContext):
    return _set_card_status(ctx, "active", "unfrozen")


@app.get("/health")
def health():
    missing = _missing_config()
    if missing:
        return jsonify({"status": "misconfigured", "missing": missing}), 503
    return jsonify({"status": "ok"})


# Vercel's Python runtime imports this module and calls the WSGI `app`
# object directly — no app.run() needed (and none should run in serverless).
