"""Feeless402 A2A agent — READ-ONLY, no wallet.

Public Agent2Agent (A2A protocol 0.3.0) endpoint at https://feeless402.com/a2a
so any A2A client can ask, in one delegated message, what an x402 call costs
on every rail its server offers. Same read-only tools as the remote MCP
endpoint (mcp_remote.py); it holds no keys and can spend nothing. The agent
replies instantly with a Message — it never creates long-running tasks.

Agent card: https://feeless402.com/.well-known/agent-card.json (static copy
in the site root; also served here at GET /.well-known/agent-card.json).

Run:  python -m nano_pay.a2a_remote   (127.0.0.1:8404, JSON-RPC at POST /a2a)
"""

import json
import re
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__, raw_to_xno
from .mcp_remote import FAUCET_ADDR, FAUCET_CLAIM_XNO, _fetch_402
from .rpc import RPC
from .x402 import collect_offers, compare_rails, offer_amount_raw, parse_quote, pick_nano_offer

AGENT_URL = "https://feeless402.com/a2a"
URL_RE = re.compile(r"https?://[^\s\"'<>]+")

CARD = {
    "protocolVersion": "0.3.0",
    "name": "Feeless402 Rail Quote",
    "description": (
        "Read-only x402 pricing agent: send it any x402 (HTTP 402 Payment "
        "Required) API URL and it fetches the live payment quote without "
        "paying, then prices the same call on every rail the server offers — "
        "gas rails floor micro-prices; the feeless Nano (XNO) rail quotes the "
        "true metered amount. Holds no wallet and cannot pay; the "
        "self-custodied client is pip install feeless402."
    ),
    "url": AGENT_URL,
    "preferredTransport": "JSONRPC",
    "version": __version__,
    "provider": {"organization": "Feeless402", "url": "https://feeless402.com"},
    "documentationUrl": "https://feeless402.com/docs.html",
    "capabilities": {
        "streaming": False,
        "pushNotifications": False,
        "stateTransitionHistory": False,
    },
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["application/json", "text/plain"],
    "securitySchemes": {},
    "security": [],
    "skills": [
        {
            "id": "x402-quote-and-compare",
            "name": "Quote an x402 endpoint across rails",
            "description": (
                "Give it an x402 API URL (optionally with a JSON body). It "
                "returns the endpoint's live 402 quote — full offer menu, "
                "XNO price, destination — plus a per-rail payer-cost "
                "comparison. Read-only: nothing is paid."
            ),
            "tags": ["x402", "pricing", "402", "nano", "rails"],
            "examples": [
                "quote https://feeless402.com/premium",
                "what would https://nano-gpt.com/api/v1/chat/completions cost per call?",
            ],
            "inputModes": ["text/plain"],
            "outputModes": ["application/json", "text/plain"],
        },
        {
            "id": "faucet-info",
            "name": "Starter-funds faucet status",
            "description": (
                "Live status of the Feeless402 onboarding faucet: address, "
                "per-claim amount, remaining claims, and how an agent claims."
            ),
            "tags": ["faucet", "nano", "onboarding"],
            "examples": ["faucet status"],
            "inputModes": ["text/plain"],
            "outputModes": ["application/json", "text/plain"],
        },
    ],
}

app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)


@app.get("/.well-known/agent-card.json")
@app.get("/a2a/.well-known/agent-card.json")
def agent_card():
    return CARD


@app.get("/a2a")
def a2a_get():
    return {
        "protocol": "a2a",
        "endpoint": AGENT_URL,
        "agent_card": "https://feeless402.com/.well-known/agent-card.json",
        "usage": "POST JSON-RPC 2.0, method message/send",
    }


def _texts(message: dict) -> str:
    parts = message.get("parts") or []
    return " ".join(p.get("text", "") for p in parts if p.get("kind") == "text")


def _quote_and_compare(url: str, method: str = "GET", json_body: str = "") -> dict:
    r = _fetch_402(url, method, json_body)
    if r.status_code != 402:
        return {"url": url, "status_code": r.status_code, "note": "not a 402 endpoint"}
    quote = parse_quote(r)
    out = {"url": url, "offers": collect_offers(quote), "compare": compare_rails(quote)}
    try:
        offer = pick_nano_offer(quote)
        out["nano"] = {
            "amount_xno": raw_to_xno(offer_amount_raw(offer)),
            "pay_to": offer.get("payTo") or offer.get("pay_to"),
        }
    except Exception as e:
        out["nano"] = {"note": str(e)}
    return out


def _faucet_info() -> dict:
    info = RPC().account_info(FAUCET_ADDR) or {}
    bal = int(info.get("balance", 0))
    per_claim = int(float(FAUCET_CLAIM_XNO) * 10**30)
    return {
        "faucet_address": FAUCET_ADDR,
        "claim_xno": FAUCET_CLAIM_XNO,
        "balance_xno": raw_to_xno(bal),
        "claims_remaining": bal // per_claim if per_claim else 0,
        "rules": "one claim per address ever; 3 per IP per day; proof-of-work required",
        "how_to_claim": "pip install feeless402; nano-pay claim https://feeless402.com",
    }


HELP = (
    "Send me an x402 API URL and I return its live 402 quote plus a per-rail "
    "cost comparison, without paying. Say 'faucet' for starter-funds info. "
    "I am read-only and hold no wallet — the self-custodied client is "
    "pip install feeless402."
)


def _handle_message(message: dict) -> list:
    """Return A2A parts answering one inbound message."""
    text = _texts(message)
    data_parts = [p.get("data") or {} for p in (message.get("parts") or []) if p.get("kind") == "data"]
    url = next((d["url"] for d in data_parts if isinstance(d, dict) and d.get("url")), None)
    if not url:
        m = URL_RE.search(text)
        url = m.group(0).rstrip(".,;)]") if m else None
    if url:
        body = next((json.dumps(d["json_body"]) for d in data_parts
                     if isinstance(d, dict) and d.get("json_body")), "")
        method = next((d["method"] for d in data_parts
                       if isinstance(d, dict) and d.get("method")), "GET")
        try:
            result = _quote_and_compare(url, method, body)
        except ValueError as e:  # SSRF guard / bad URL — refusal is the answer
            result = {"url": url, "error": str(e)}
        summary = (
            f"{result['nano']['amount_xno']} XNO per call on nano:mainnet"
            if isinstance(result.get("nano"), dict) and result["nano"].get("amount_xno")
            else result.get("error") or result.get("note") or "see data"
        )
        return [
            {"kind": "data", "data": result},
            {"kind": "text", "text": f"Quote for {url}: {summary}."},
        ]
    if "faucet" in text.lower():
        info = _faucet_info()
        return [
            {"kind": "data", "data": info},
            {"kind": "text", "text": f"Faucet holds {info['balance_xno']} XNO "
                                     f"({info['claims_remaining']} claims left)."},
        ]
    return [{"kind": "text", "text": HELP}]


def _rpc_error(rid, code: int, msg: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}},
                        status_code=status)


@app.post("/a2a")
async def a2a_rpc(request: Request):
    try:
        req = await request.json()
    except Exception:
        return _rpc_error(None, -32700, "parse error", 400)
    rid = req.get("id")
    method = req.get("method")
    if method != "message/send":
        if method in ("tasks/get", "tasks/cancel", "tasks/resubscribe"):
            return _rpc_error(rid, -32001, "task not found — this agent replies "
                                           "instantly with messages and creates no tasks")
        return _rpc_error(rid, -32601, f"method not supported: {method}")
    message = (req.get("params") or {}).get("message") or {}
    try:
        parts = _handle_message(message)
    except Exception as e:  # never leak a traceback to the wire
        return _rpc_error(rid, -32603, f"internal error: {type(e).__name__}")
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "result": {
            "kind": "message",
            "role": "agent",
            "messageId": str(uuid.uuid4()),
            "contextId": message.get("contextId") or str(uuid.uuid4()),
            "parts": parts,
        },
    }


def main():
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8404, log_level="info")


if __name__ == "__main__":
    main()
