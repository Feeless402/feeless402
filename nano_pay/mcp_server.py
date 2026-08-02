"""Feeless402 MCP server — feeless x402 micropayments as agent tools.

Any MCP-speaking agent (Claude, Cursor, OpenClaw bridges, ...) can load
this and gain the ability to price, pay for, and earn from x402 APIs
over the feeless rail.

Run:  nano-pay mcp            (stdio transport)
Config example (Claude Code):
  claude mcp add feeless402 -- nano-pay mcp
"""

import json

from mcp.server.mcpserver import MCPServer

from . import raw_to_xno, xno_to_raw
from .rpc import RPC
from .wallet import Wallet

INSTRUCTIONS = """Pay for x402-priced HTTP APIs (HTTP 402 Payment Required)
with feeless, sub-second Nano (XNO) micropayments from a self-custodied
local wallet.

The economics, verifiable via x402_compare: gas rails floor micro-prices
(e.g. 0.001 USDC minimum ~ 370x a sub-millicent call); the feeless rail
quotes the true metered price. Strategy: top up once (or claim free
starter XNO from a faucet), then micropay per call at marginal cost zero.

Safety: payments are refused above max_xno (default 0.05). The wallet
seed never leaves the local machine and is never returned by any tool.
Keep only working capital in this wallet."""

mcp = MCPServer(
    name="feeless402",
    instructions=INSTRUCTIONS,
    website_url="https://feeless402.com",
)


def _rpc():
    return RPC()


@mcp.tool(description="Wallet address, XNO balance, and pending incoming amounts")
def wallet_status() -> str:
    w = Wallet().load() if Wallet().exists() else Wallet().create()
    rpc = _rpc()
    acct = w.synced_account(rpc)
    pending = rpc.receivable(w.address)
    return json.dumps(
        {
            "address": w.address,
            "balance_xno": raw_to_xno(acct.raw_bal),
            "receivable_xno": raw_to_xno(sum(pending.values())),
        }
    )


@mcp.tool(
    description="Fetch an x402 endpoint's payment quote WITHOUT paying. "
    "Returns price in XNO, destination, and the full offer menu."
)
def x402_quote(url: str, method: str = "GET", json_body: str = "") -> str:
    from .x402 import request_with_payment

    kwargs = {"json": json.loads(json_body)} if json_body else {}
    if json_body and method == "GET":
        method = "POST"
    r, info = request_with_payment(
        method, url, None, _rpc(), max_raw=0, dry_run=True, **kwargs
    )
    if info is None:
        return json.dumps({"status_code": r.status_code, "note": "not a 402 endpoint"})
    return json.dumps(
        {
            "amount_xno": info["amount_xno"],
            "pay_to": info["pay_to"],
            "offer": info["offer"],
        }
    )


@mcp.tool(
    description="Price the same API call on EVERY rail the server offers "
    "(Base/Solana USDC, Lightning, XNO...) from live data — use this to "
    "verify which rail is cheapest instead of trusting documentation."
)
def x402_compare(url: str, method: str = "GET", json_body: str = "") -> str:
    import requests as rq

    from .x402 import compare_rails, parse_quote

    kwargs = {"json": json.loads(json_body)} if json_body else {}
    if json_body and method == "GET":
        method = "POST"
    r = rq.request(method, url, headers={"x-x402": "true"}, timeout=60, **kwargs)
    if r.status_code != 402:
        return json.dumps({"status_code": r.status_code, "note": "not a 402 endpoint"})
    return json.dumps(compare_rails(parse_quote(r)))


@mcp.tool(
    description="Request a URL and automatically pay its Nano x402 quote "
    "(feeless, sub-second). Refuses quotes above max_xno. Returns the "
    "paid response body and the settlement receipt."
)
def x402_pay(
    url: str, method: str = "GET", json_body: str = "", max_xno: str = "0.05"
) -> str:
    from .x402 import request_with_payment

    w = Wallet().load()
    kwargs = {"json": json.loads(json_body)} if json_body else {}
    if json_body and method == "GET":
        method = "POST"
    r, receipt = request_with_payment(
        method, url, w, _rpc(), max_raw=xno_to_raw(max_xno), **kwargs
    )
    try:
        body = r.json()
    except Exception:
        body = r.text[:2000]
    return json.dumps(
        {"status_code": r.status_code, "receipt": receipt, "body": body}
    )


@mcp.tool(
    description="Claim free starter XNO from a Feeless402 faucet (solves "
    "the faucet's proof-of-work challenge automatically; may take ~1 min "
    "of CPU). Use when the wallet is empty."
)
def faucet_claim(faucet_url: str = "https://feeless402.com") -> str:
    from .cli import _claim_one

    w = Wallet().load() if Wallet().exists() else Wallet().create()
    code, res = _claim_one(faucet_url, w)
    if code == 200:
        got = w.receive_all(_rpc())
        res["received_xno"] = raw_to_xno(sum(a for _, a in got))
    return json.dumps({"status": code, "result": res})


@mcp.tool(
    description="Quote a small swap from another asset (e.g. USDC-BASE, "
    "USDT-SOL) into XNO for topping up the wallet. Execution requires "
    "NANSWAP_API_KEY; this tool only quotes."
)
def topup_quote(amount: float = 5.0, asset: str = "USDC-BASE") -> str:
    from .topup import estimate, limits

    w = Wallet().load() if Wallet().exists() else Wallet().create()
    return json.dumps(
        {
            "estimate": estimate(asset, amount),
            "limits": limits(asset),
            "receive_address": w.address,
        }
    )


def main():
    mcp.run("stdio")


if __name__ == "__main__":
    main()
