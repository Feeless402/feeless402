"""x402 client for the Nano `exact` scheme.

Speaks both header dialects seen in the wild:
- x402 v2 (x402nano spec): 402 carries `PAYMENT-REQUIRED` header (base64
  JSON); client replies with `PAYMENT-SIGNATURE` header.
- x402 v1 (NanoGPT-style): 402 carries a JSON body with `accepts`;
  client replies with `X-PAYMENT` header.
We parse whichever is present and send the payment in both headers.
"""

import base64
import json

import requests

from . import raw_to_xno


class X402Error(Exception):
    pass


class PriceCapExceeded(X402Error):
    pass


def _b64_json(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def _from_b64_json(s: str):
    return json.loads(base64.b64decode(s))


def parse_quote(resp) -> dict:
    """Extract the PaymentRequirements object from a 402 response."""
    hdr = resp.headers.get("payment-required") or resp.headers.get(
        "x-payment-required"
    )
    if hdr:
        try:
            return _from_b64_json(hdr)
        except Exception:
            try:
                return json.loads(hdr)
            except Exception as e:
                raise X402Error(f"unparseable payment-required header: {e}")
    try:
        return resp.json()
    except Exception:
        raise X402Error(
            f"402 response with no parseable quote "
            f"(headers: {list(resp.headers)}, body: {resp.text[:200]})"
        )


def collect_offers(quote: dict) -> list:
    """All offers across both dialects (`accepts` and `payment.accepted`)."""
    offers = list(quote.get("accepts") or [])
    offers += list(quote.get("accepted") or [])
    offers += list((quote.get("payment") or {}).get("accepted") or [])
    return offers


def compare_rails(quote: dict) -> list:
    """Normalized per-rail price rows from a 402 quote, for side-by-side
    comparison. Purely informational — lets a client verify rail-cost
    claims from live data instead of trusting documentation."""
    rows = []
    for o in collect_offers(quote):
        amount = None
        for f in ("amount", "maxAmountRequired", "max_amount_required"):
            if f in o:
                amount = str(o[f])
                break
        formatted = (
            o.get("amountFormatted")
            or o.get("maxAmountRequiredFormatted")
            or amount
        )
        usd = o.get("amountUsd") or o.get("maxAmountRequiredUSD")
        network = str(o.get("network", "?"))
        if network.startswith("nano") and amount and amount.isdigit():
            formatted = f"{raw_to_xno(int(amount))} XNO"
        asset = o.get("asset") or (o.get("extra") or {}).get("tokenSymbol")
        # What actually leaves the payer's wallet, in USD — for USD-pegged
        # tokens that's the transfer amount itself (6 decimals), which can
        # exceed the metered service cost on floored rails. For other
        # rails, fall back to the server-reported USD figure.
        transfer_usd = usd
        symbol = str(asset or "") + str(o.get("scheme", ""))
        if "usdc" in symbol.lower() or "usdt" in symbol.lower() or (
            formatted and "USDC" in str(formatted)
        ):
            if amount and str(amount).isdigit():
                transfer_usd = int(amount) / 1e6
        row = {
            "scheme": o.get("scheme"),
            "network": network,
            "asset": asset,
            "payer_sends": formatted,
            "service_cost_usd": usd,
            "payer_cost_usd": transfer_usd,
            "feeless": network.startswith("nano"),
        }
        key = (network, str(formatted))
        if key not in {(r["network"], str(r["payer_sends"])) for r in rows}:
            rows.append(row)
    priced = [r for r in rows if r["payer_cost_usd"]]
    if priced:
        cheapest = min(priced, key=lambda r: float(r["payer_cost_usd"]))
        for r in priced:
            ratio = float(r["payer_cost_usd"]) / float(cheapest["payer_cost_usd"])
            r["vs_cheapest"] = "cheapest" if r is cheapest else f"{ratio:,.0f}x more"
    return rows


def pick_nano_offer(quote: dict) -> dict:
    accepts = collect_offers(quote)
    nano_offers = [
        o
        for o in accepts
        if str(o.get("network", "")).lower().startswith("nano")
        or str(o.get("asset", "")).lower() in ("xno", "nano")
    ]
    if not nano_offers:
        raise X402Error(
            "no Nano payment option offered; server accepts: "
            + ", ".join(
                f"{o.get('scheme')}/{o.get('network')}/{o.get('asset')}"
                for o in accepts
            )
        )
    # Prefer the x402 `exact` scheme (signed block in header, server
    # settles) over manual pay-then-callback schemes.
    for o in nano_offers:
        if "exact" in (
            str(o.get("protocolScheme", "")).lower()
            + str(o.get("scheme", "")).lower()
        ):
            return o
    return nano_offers[0]


def offer_amount_raw(offer: dict) -> int:
    for field in ("amount", "maxAmountRequired", "max_amount_required"):
        if field in offer:
            return int(offer[field])
    raise X402Error(f"no amount field in offer: {offer}")


def offer_pay_to(offer: dict) -> str:
    for field in ("payTo", "payToAddress", "pay_to", "destination"):
        if field in offer and str(offer[field]).startswith(
            ("nano_", "xrb_")
        ):
            return offer[field]
    raise X402Error(f"no payTo nano address in offer: {offer}")


def build_payment_header(quote: dict, offer: dict, block_dict: dict, pay_to: str) -> str:
    block = dict(block_dict)
    block["link_as_account"] = pay_to  # NanoGPT requires it alongside link

    inner = {"block": block}
    if offer.get("paymentId"):
        inner["paymentId"] = offer["paymentId"]

    if "paymentId" in offer:
        # NanoGPT dialect: minimal x402 v1 payload, protocol scheme name.
        payload = {
            "x402Version": 1,
            "scheme": offer.get("protocolScheme", "exact"),
            "network": offer.get("network", "nano:mainnet"),
            "payload": inner,
        }
    else:
        # x402nano spec (v2): echo the accepted requirements.
        payload = {
            "x402Version": quote.get("x402Version", 2),
            "accepted": offer,
            "scheme": offer.get("scheme", "exact"),
            "network": offer.get("network", "nano:mainnet"),
            "payload": inner,
        }
        if "resource" in quote:
            payload["resource"] = quote["resource"]
    return _b64_json(payload)


def request_with_payment(
    method: str,
    url: str,
    wallet,
    rpc,
    max_raw: int,
    headers: dict = None,
    dry_run: bool = False,
    prework: bool = False,
    **req_kwargs,
):
    """Make an HTTP request, transparently paying a Nano x402 quote.

    Returns (response, receipt_dict_or_None). dry_run=True stops after
    the quote and returns (402_response, parsed_quote).

    prework defaults to False so this returns as soon as the payment has
    settled. Setting it True makes the call block for minutes afterwards
    solving the *next* block's proof-of-work — never do that inside a
    request handler. To keep later payments fast, call
    `wallet.prework(wallet._work_root(wallet.synced_account(rpc)), rpc)`
    yourself once the caller has their response.
    """
    headers = dict(headers or {})
    # Signal x402 support (NanoGPT requires opting in to the quote flow).
    headers.setdefault("x-x402", "true")

    r = requests.request(method, url, headers=headers, timeout=60, **req_kwargs)
    if r.status_code != 402:
        return r, None

    quote = parse_quote(r)
    offer = pick_nano_offer(quote)
    amount = offer_amount_raw(offer)
    pay_to = offer_pay_to(offer)

    if dry_run:
        return r, {
            "quote": quote,
            "offer": offer,
            "amount_raw": amount,
            "amount_xno": raw_to_xno(amount),
            "pay_to": pay_to,
        }

    if amount > max_raw:
        raise PriceCapExceeded(
            f"quote {raw_to_xno(amount)} XNO exceeds cap "
            f"{raw_to_xno(max_raw)} XNO — refusing to pay"
        )

    block, new_frontier, work_root = wallet.build_payment_block(
        rpc, pay_to, amount
    )
    pay_header = build_payment_header(quote, offer, block, pay_to)
    headers["PAYMENT-SIGNATURE"] = pay_header
    headers["X-PAYMENT"] = pay_header

    r2 = requests.request(
        method, url, headers=headers, timeout=120, **req_kwargs
    )
    receipt = None
    rec_hdr = r2.headers.get("payment-response") or r2.headers.get(
        "x-payment-response"
    )
    if rec_hdr:
        try:
            receipt = _from_b64_json(rec_hdr)
        except Exception:
            receipt = {"raw_header": rec_hdr}

    if 200 <= r2.status_code < 300:
        wallet.payment_succeeded(rpc, new_frontier, work_root, prework=prework)
    else:
        wallet.payment_failed(work_root)
    return r2, receipt or {
        "amount_xno": raw_to_xno(amount),
        "pay_to": pay_to,
        "settled": 200 <= r2.status_code < 300,
    }
