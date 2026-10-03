"""x402 client for the Nano `exact` scheme.

Speaks both header dialects seen in the wild:
- x402 v2 (x402nano spec): 402 carries `PAYMENT-REQUIRED` header (base64
  JSON); client replies with `PAYMENT-SIGNATURE` header.
- x402 v1 (NanoGPT-style): 402 carries a JSON body with `accepts`;
  client replies with `X-PAYMENT` header.
We parse whichever is present and send the payment in both headers.
"""

import base64
import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from . import raw_to_xno
from .rpc import says_block_not_found
from .verify import CONFIRM_WAIT_S


class X402Error(Exception):
    pass


class PriceCapExceeded(X402Error):
    pass


class PaidRequestFailed(X402Error):
    """The request failed AFTER a signed payment block was handed to the
    merchant and no HTTP response came back. `receipt` carries the block
    hash and the ledger's verdict: settled True means the money moved and
    only the reply was lost — re-present the SAME block, never pay again;
    "indeterminate" means the ledger does not show it yet — check the hash
    before doing anything else. An outcome that could not be determined is
    never reported as "did not happen" (x402 #3208)."""

    def __init__(self, msg, receipt):
        super().__init__(msg)
        self.receipt = receipt


# The ledger could not be asked at all. Distinct from None ("the ledger
# answered: no such block"): "could not ask" is never proof the money did not
# move, so it must never lead to signing a second payment. Truthy on purpose —
# no caller may treat it as "absent" by accident; each one checks for it.
UNREACHABLE = "unreachable"


def _ledger_verdict(rpc, block_hash: str, wait: float = 0.0):
    """Ask the ledger about a block we signed. Returns "confirmed", "present"
    (seen, not yet confirmed), None (the ledger answered that it has no such
    block) or UNREACHABLE (no answer on the last attempt). `wait` bounds how
    long to keep looking for a block that may still be propagating."""
    import time

    deadline = time.time() + wait
    seen = None            # once a block is seen it stays seen; a later error cannot unsee it
    unreachable = False
    while True:
        try:
            info = rpc.call(
                {"action": "block_info", "json_block": "true", "hash": block_hash}
            )
            unreachable = False
            if isinstance(info, dict) and "contents" in info:
                seen = (
                    "confirmed"
                    if str(info.get("confirmed")).lower() == "true"
                    else (seen or "present")
                )
        except Exception as e:
            unreachable = not says_block_not_found(e)
        if seen == "confirmed" or time.time() >= deadline:
            if seen:
                return seen
            return UNREACHABLE if unreachable else None
        time.sleep(0.5)


def _settle_outcome(rpc, block_hash: str, status_code):
    """Decide what to tell the caller once the merchant has answered (or
    failed to). The ledger, not the HTTP reply, is the truth.

    Returns (settled, ledger): settled is True, False or "indeterminate".
    Only a ledger that ANSWERED "no such block" after a refusal yields False;
    a ledger we could not ask always yields "indeterminate".
    """
    # One budget for every branch (reported by pyfile-toolkit, PR #13): a
    # block that just landed is exactly one the node has not indexed yet, and
    # a refusal is where a landed block most needs to be caught.
    ledger = _ledger_verdict(rpc, block_hash, wait=CONFIRM_WAIT_S)
    if ledger in ("confirmed", "present"):
        return True, ledger
    if ledger == UNREACHABLE:
        # Reported by pyfile-toolkit (PRs #10, #13) and enricoaboujaoude-droid (#18).
        return "indeterminate", UNREACHABLE
    if status_code is not None and 200 <= status_code < 300:
        # A 2xx is the merchant's word, not the ledger's. A forged or mistaken
        # success must not become a receipt that says "paid" (declared_safe
        # mode of the #3208 retry-safety battery).
        return "indeterminate", "absent"
    # An explicit 402 is a refusal — the merchant says it never broadcast —
    # and the ledger agrees.
    if status_code == 402:
        return False, "absent"
    return "indeterminate", "absent"


def _b64_json(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def _from_b64_json(s: str):
    return json.loads(base64.b64decode(s))


_OFFER_LIST_KEYS = ("accepts", "accepted", "payment")


def _parse_header_quote(hdr: str):
    try:
        return _from_b64_json(hdr)
    except Exception:
        try:
            return json.loads(hdr)
        except Exception as e:
            raise X402Error(f"unparseable payment-required header: {e}")


def parse_quote(resp) -> dict:
    """Extract the PaymentRequirements object from a 402 response.

    Merchants disagree about where the quote lives: some put the whole
    requirements object in the `payment-required` header, some put it in
    the JSON body, and some (NanoGPT since Sep 2026) put a *single* offer
    in the header while the body carries the full `accepts` list. Read
    both and union the offers so no rail the server actually accepts is
    hidden from `pick_nano_offer`.
    """
    hdr = resp.headers.get("payment-required") or resp.headers.get(
        "x-payment-required"
    )
    hdr_quote = _parse_header_quote(hdr) if hdr else None
    if isinstance(hdr_quote, dict) and not any(
        k in hdr_quote for k in _OFFER_LIST_KEYS
    ):
        # A bare offer object, not a requirements envelope: wrap it.
        if "scheme" in hdr_quote or "payTo" in hdr_quote:
            hdr_quote = {"x402Version": 1, "accepts": [hdr_quote]}

    body_quote = None
    try:
        body_quote = resp.json()
    except Exception:
        pass
    if not isinstance(body_quote, dict) or not collect_offers(body_quote):
        body_quote = None

    if hdr_quote is None and body_quote is None:
        raise X402Error(
            f"402 response with no parseable quote "
            f"(headers: {list(resp.headers)}, body: {resp.text[:200]})"
        )
    if body_quote is None:
        return hdr_quote
    if hdr_quote is None or not collect_offers(hdr_quote):
        return body_quote

    # Both carry offers: keep the body envelope (it has the richer
    # paymentId/statusUrl fields) and append header offers it lacks.
    merged = dict(body_quote)

    def _key(o):
        return o.get("paymentId") or json.dumps(o, sort_keys=True)

    seen = {_key(o) for o in collect_offers(merged)}
    extra = [o for o in collect_offers(hdr_quote) if _key(o) not in seen]
    if extra:
        merged["accepts"] = list(merged.get("accepts") or []) + extra
    return merged


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


# --- retry safety (GHSA-cx37-j5vc-c967, reported privately against 0.2.8) ----------------------------------------
# A retry after a lost reply signed a SECOND block: one request, two charges. Now every payment is written to a small
# journal next to the wallet BEFORE it is sent, and a retry — inside the same call or in a later call after a crash —
# re-presents that same signed block. The merchant then either settles it (first time it arrives) or honors it as
# already settled (x402 #3325 §5.3.5). A second block is signed only when the ledger shows the first never landed.
JOURNAL_TTL_S = 24 * 3600
SEND_ATTEMPTS = 3            # the first send plus two re-presentations of the same block within one call


def _journal_path(wallet) -> Path:
    base = Path(getattr(wallet, "path", "") or (Path.home() / ".nano-pay" / "wallet.json"))
    return base.parent / "pending-payments.json"


def _journal_load(wallet) -> dict:
    """The record of payments already signed. No file = nothing pending. A file
    that exists but cannot be read FAILS CLOSED: an empty journal would forget a
    block that may already have paid, and the next call would sign another one
    (reported by enricoaboujaoude-droid, PR #16)."""
    p = _journal_path(wallet)
    if not p.exists():
        return {}
    try:
        j = json.loads(p.read_text())
        if not isinstance(j, dict):
            raise ValueError("not a JSON object")
    except Exception as e:
        raise X402Error(f"payment journal {p} cannot be read ({e}); refusing to pay so an already-signed "
                        f"payment is not forgotten. Inspect the file, then move it aside to continue.") from e
    now = time.time()
    return {k: v for k, v in j.items() if isinstance(v, dict) and now - float(v.get("t", 0)) < JOURNAL_TTL_S}


def _journal_save(wallet, j: dict, required: bool = False) -> None:
    """required=True is the write BEFORE a payment leaves: if the block cannot be
    recorded, it is not sent (reported by enricoaboujaoude-droid, PR #15). Later
    writes only tidy up — re-presenting a block never pays twice — so they
    may fail quietly."""
    p = _journal_path(wallet)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(j, indent=1))
        os.replace(tmp, p)
    except Exception as e:
        if required:
            raise X402Error(f"payment journal {p} cannot be written ({e}); refusing to send a payment "
                            f"that could not be re-presented after a lost reply.") from e


def _request_fingerprint(req_kwargs: dict) -> str:
    """What the request carries besides its URL. Two different operations at the
    same endpoint and price must not share a journal entry, or a retry could
    re-present one operation's payment for another (reported by
    enricoaboujaoude-droid, PR #17). Empty when there is no body or params, so
    the key for a plain GET is unchanged from earlier versions."""
    parts = []
    for name in ("params", "json", "data"):
        v = req_kwargs.get(name)
        if v is None:
            continue
        if isinstance(v, bytes):
            v = v.decode("utf-8", "replace")
        elif not isinstance(v, str):
            v = json.dumps(v, sort_keys=True, default=str)
        parts.append(f"{name}={v}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest() if parts else ""


def _journal_key(method: str, url: str, pay_to: str, amount: int, fingerprint: str = "") -> str:
    base = f"{method.upper()}|{url}|{pay_to}|{amount}"
    if fingerprint:
        base += f"|{fingerprint}"
    return hashlib.sha256(base.encode()).hexdigest()[:32]


def _replay_message(block_hash: str, method: str, path: str) -> bytes:
    return f"feeless402-replay:{block_hash.upper()}:{method.upper()}:{path or '/'}".encode()


def replay_proof(account, block_hash: str, method: str, path: str) -> str:
    """Proof that the party re-presenting a settled block is the one who signed it: the payer's own key signs
    (block hash, method, path). A settled block is public, so anyone can re-present it; only the payer can sign
    this — which is what stops an observer from using up the payer's retries. Sent as X-PAYMENT-PROOF."""
    import nanopy
    sig = nanopy.ext.sign(bytes.fromhex(account._sk), _replay_message(block_hash, method, path), os.urandom(32))
    return base64.b64encode(json.dumps({"hash": block_hash.upper(), "sig": bytes(sig).hex()}).encode()).decode()


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

    jkey = _journal_key(method, url, pay_to, amount, _request_fingerprint(req_kwargs))
    journal = _journal_load(wallet)
    entry = journal.get(jkey)
    represented = bool(entry)
    if entry:
        # A payment for this exact request is already signed and may already have settled: re-present it.
        pay_header, new_frontier, work_root = entry["header"], entry["hash"], entry.get("work_root", "")
    else:
        block, new_frontier, work_root = wallet.build_payment_block(
            rpc, pay_to, amount
        )
        pay_header = build_payment_header(quote, offer, block, pay_to)
        journal[jkey] = {"header": pay_header, "hash": new_frontier, "work_root": work_root,
                         "url": url, "method": method.upper(), "t": time.time()}
        try:
            _journal_save(wallet, journal, required=True)   # recorded BEFORE it leaves: a crash mid-send still re-presents it
        except X402Error:
            wallet.payment_failed(work_root)                # the block never left; release its work
            raise
    headers["PAYMENT-SIGNATURE"] = pay_header
    headers["X-PAYMENT"] = pay_header
    try:
        headers["X-PAYMENT-PROOF"] = replay_proof(wallet.account(), new_frontier, method, urlparse(url).path)
    except Exception:
        pass

    r2, last_err = None, None
    for attempt in range(SEND_ATTEMPTS):
        try:
            r2 = requests.request(
                method, url, headers=headers, timeout=120, **req_kwargs
            )
            break
        except requests.RequestException as e:
            last_err = e
            if attempt + 1 < SEND_ATTEMPTS:
                time.sleep(2 * (attempt + 1))   # same block again, never a new one
    if r2 is None:
        e = last_err
        # The block is in the merchant's hands and we got no answer. The
        # money may well have moved — only the ledger knows. The journal keeps
        # the block, so the next call for this request re-presents it.
        settled, ledger = _settle_outcome(rpc, new_frontier, None)
        if settled is True:
            wallet.payment_succeeded(rpc, new_frontier, work_root, prework=False)
        else:
            wallet.payment_failed(work_root)
        base = {
            "amount_xno": raw_to_xno(amount),
            "pay_to": pay_to,
            "block": new_frontier,
            "settled": settled,
            "ledger": ledger,
            "note": _OUTCOME_NOTE[settled],
            "will_re_present": True,
        }
        raise PaidRequestFailed(f"no reply from merchant: {e}", base) from e

    if represented and r2.status_code == 402:
        verdict = _ledger_verdict(rpc, new_frontier, wait=CONFIRM_WAIT_S)
        if verdict == UNREACHABLE:
            # The merchant refused the block we re-presented and the ledger
            # cannot be asked whether it landed. Signing a fresh payment now
            # could pay twice; stop, keep the journal entry, let the caller retry.
            raise PaidRequestFailed("merchant refused and the ledger could not be checked; not paying again",
                                    {"amount_xno": raw_to_xno(amount), "pay_to": pay_to, "block": new_frontier,
                                     "settled": "indeterminate", "ledger": UNREACHABLE,
                                     "note": _OUTCOME_NOTE["indeterminate"], "will_re_present": True})
        if verdict:
            # We paid (the ledger says so) and the merchant will not honor it: say so. Never pay twice.
            raise PaidRequestFailed("merchant refused a payment that is already on the ledger (paid, not served)",
                                    {"amount_xno": raw_to_xno(amount), "pay_to": pay_to, "block": new_frontier,
                                     "settled": True, "ledger": verdict, "note": _OUTCOME_NOTE[True]})
        # The earlier block never landed (and cannot now: a stale frontier). Only now is a fresh payment right.
        journal.pop(jkey, None)
        _journal_save(wallet, journal)
        headers.pop("X-PAYMENT-PROOF", None)
        return request_with_payment(method, url, wallet, rpc, max_raw, headers=headers,
                                    dry_run=dry_run, prework=prework, **req_kwargs)

    if 200 <= r2.status_code < 300 or r2.status_code == 402:
        journal.pop(jkey, None)                 # served, or refused outright: nothing left to re-present
        _journal_save(wallet, journal)

    receipt = None
    rec_hdr = r2.headers.get("payment-response") or r2.headers.get(
        "x-payment-response"
    )
    if rec_hdr:
        try:
            receipt = _from_b64_json(rec_hdr)
        except Exception:
            receipt = {"raw_header": rec_hdr}

    settled, ledger = _settle_outcome(rpc, new_frontier, r2.status_code)
    if settled is True:
        wallet.payment_succeeded(rpc, new_frontier, work_root, prework=prework)
    else:
        wallet.payment_failed(work_root)
    # What we paid is ours to know regardless of what the merchant echoes
    # back — merge our ground truth under their receipt fields.
    base = {
        "amount_xno": raw_to_xno(amount),
        "pay_to": pay_to,
        "block": new_frontier,
        "settled": settled,
        "ledger": ledger,
    }
    if settled is not True:
        base["note"] = _OUTCOME_NOTE[settled]
    if receipt:
        # The merchant's receipt must be about OUR block. A different hash
        # means the receipt is forged or belongs to someone else's payment;
        # keep their fields for the record but never let them speak for ours.
        theirs = str(receipt.get("hash") or receipt.get("transaction") or "").upper()
        if theirs and theirs != new_frontier.upper():
            base["receipt_hash_mismatch"] = True
            base["note"] = ("merchant receipt names a different block than the one "
                            "we signed — treat the receipt as untrusted; "
                            "the ledger verdict above is what counts")
        base.update({k: v for k, v in receipt.items()
                     if k not in ("settled", "block", "amount_xno", "note",
                                  "ledger", "receipt_hash_mismatch")})
        base["amount_xno"] = raw_to_xno(amount)
        base["block"] = new_frontier
    return r2, base


_OUTCOME_NOTE = {
    True: "payment is on the ledger; if the merchant did not deliver, "
          "re-present this SAME block — do not pay again",
    False: "merchant refused and the ledger does not hold the block; "
           "nothing was paid",
    "indeterminate": "no confirmation from merchant or ledger; check this "
                     "block hash before paying again — do NOT re-pay blind",
}
