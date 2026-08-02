"""Feeless402 remote MCP endpoint — READ-ONLY demo, no wallet.

Public streamable-HTTP MCP server at https://feeless402.com/mcp so any
MCP client can price x402 calls across rails with zero install. It holds
no keys and can spend nothing; every tool is a read of public data. The
full experience (paying, earning, claiming) lives in the local package:
    pip install feeless402   ->   nano-pay mcp   (stdio, self-custodied)

Run:  python -m nano_pay.mcp_remote   (127.0.0.1:8403, path /mcp)
"""

import ipaddress
import json
import socket
from urllib.parse import urlparse

import requests

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from . import raw_to_xno
from .rpc import RPC
from .x402 import collect_offers, compare_rails, offer_amount_raw, parse_quote, pick_nano_offer

FAUCET_ADDR = "nano_1hk1cu3773u5r39e75mtqrauzro75j3hwdzyewz8izokzur66semy739w14h"
FAUCET_CLAIM_XNO = "0.005"

INSTRUCTIONS = """Read-only demo of the Feeless402 rail: price any x402
(HTTP 402 Payment Required) API across payment rails and see what the
same call costs on each — gas rails floor micro-prices; the feeless Nano
rail quotes the true metered amount.

This remote endpoint holds no wallet and cannot pay. To actually pay,
earn, or claim starter funds, run the local server (self-custodied seed,
never leaves your machine):  pip install feeless402  ->  nano-pay mcp"""

mcp = MCPServer(
    name="feeless402-demo",
    instructions=INSTRUCTIONS,
    website_url="https://feeless402.com",
)


def _guard(url: str) -> None:
    """Refuse URLs that aren't public http(s) — this server fetches them."""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError("only http(s) URLs are allowed")
    host = p.hostname or ""
    try:
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80))
    except socket.gaierror:
        raise ValueError(f"cannot resolve host: {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError("URL resolves to a non-public address")


def _fetch_402(url: str, method: str, json_body: str):
    _guard(url)
    kwargs = {"json": json.loads(json_body)} if json_body else {}
    if json_body and method == "GET":
        method = "POST"
    return requests.request(
        method, url, headers={"x-x402": "true"},
        timeout=30, allow_redirects=False, **kwargs,
    )


@mcp.tool(
    description="Fetch an x402 endpoint's payment quote WITHOUT paying: "
    "price in XNO, destination, and the full offer menu across all rails."
)
def x402_quote(url: str, method: str = "GET", json_body: str = "") -> str:
    r = _fetch_402(url, method, json_body)
    if r.status_code != 402:
        return json.dumps({"status_code": r.status_code, "note": "not a 402 endpoint"})
    quote = parse_quote(r)
    out = {"offers": collect_offers(quote)}
    try:
        offer = pick_nano_offer(quote)
        out["nano"] = {
            "amount_xno": raw_to_xno(offer_amount_raw(offer)),
            "pay_to": offer.get("payTo") or offer.get("pay_to"),
        }
    except Exception as e:
        out["nano"] = {"note": str(e)}
    return json.dumps(out)


@mcp.tool(
    description="Price the same API call on EVERY rail the server offers "
    "(Base/Solana USDC, Lightning, XNO...) from live data — the payer-cost "
    "arithmetic agents use to pick a rail."
)
def x402_compare(url: str, method: str = "GET", json_body: str = "") -> str:
    r = _fetch_402(url, method, json_body)
    if r.status_code != 402:
        return json.dumps({"status_code": r.status_code, "note": "not a 402 endpoint"})
    return json.dumps(compare_rails(parse_quote(r)))


@mcp.tool(
    description="Live status of the Feeless402 starter faucet: address, "
    "per-claim amount, remaining balance, and how an agent claims."
)
def faucet_info() -> str:
    rpc = RPC()
    info = rpc.account_info(FAUCET_ADDR) or {}
    bal = int(info.get("balance", 0))
    per_claim = int(float(FAUCET_CLAIM_XNO) * 10**30)
    return json.dumps(
        {
            "faucet_address": FAUCET_ADDR,
            "claim_xno": FAUCET_CLAIM_XNO,
            "balance_xno": raw_to_xno(bal),
            "claims_remaining": bal // per_claim if per_claim else 0,
            "rules": "one claim per address ever; 3 per IP per day; proof-of-work required",
            "how_to_claim": "pip install feeless402; nano-pay claim https://feeless402.com "
            "(or the faucet_claim tool of the LOCAL MCP server: nano-pay mcp)",
            "donations": f"send XNO to {FAUCET_ADDR} — every 0.005 onboards another agent",
        }
    )


@mcp.tool(
    description="How to get the full self-custodied Feeless402 toolset "
    "(pay, earn, claim) running locally in under a minute."
)
def get_started() -> str:
    return json.dumps(
        {
            "install": "pip install feeless402   (or: uvx feeless402 mcp)",
            "mcp_local": "claude mcp add feeless402 -- uvx feeless402 mcp",
            "first_funds": "nano-pay claim https://feeless402.com  (free starter XNO, PoW-gated)",
            "first_paid_call": "nano-pay pay https://feeless402.com/premium",
            "docs": "https://feeless402.com/docs.html",
            "spec": "https://railhint.com",
            "source": "https://github.com/Feeless402/feeless402",
            "why_this_rail": "feeless + sub-second: the transfer amount IS the price; "
            "no gas floor, so sub-millicent calls cost sub-millicents",
        }
    )


def main():
    mcp.run(
        "streamable-http",
        host="127.0.0.1",
        port=8403,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        # DNS-rebinding protection is for LOCALHOST servers; on a public
        # endpoint it would reject legit gateways (arbitrary Origins).
        # nginx's vhost already pins the Host header to feeless402.com.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )


if __name__ == "__main__":
    main()
