"""Top-up: swap a small amount of another asset (USDT etc.) into XNO.

We never hold or route funds — the agent transacts directly with an
existing instant-swap service. This module just fetches quotes and,
when an API key is configured, creates the swap order.

Primary: NanSwap (Nano-ecosystem native, public API). Tickers need a
network suffix, e.g. USDC-BASE, USDT-SOL, USDT-ETH; plain XNO/BAN/BTC
stand alone. Min swap is ~$0.02; estimates need no API key.
Fallback guidance: ChangeNOW / SimpleSwap / Trocador (manual or API).
"""

import os

import requests

NANSWAP_API = "https://api.nanswap.com/v1"


class TopupError(Exception):
    pass


def _key():
    return os.environ.get("NANSWAP_API_KEY", "")


def _headers():
    k = _key()
    return {"nanswap-api-key": k} if k else {}


def estimate(from_asset: str, amount: float, to_asset: str = "XNO") -> dict:
    """Estimated XNO received for `amount` of from_asset."""
    r = requests.get(
        f"{NANSWAP_API}/get-estimate",
        params={"from": from_asset.upper(), "to": to_asset.upper(), "amount": amount},
        headers=_headers(),
        timeout=30,
    )
    data = r.json()
    if r.status_code >= 400 or "error" in data:
        raise TopupError(f"NanSwap estimate failed: {data}")
    return data


def limits(from_asset: str, to_asset: str = "XNO") -> dict:
    r = requests.get(
        f"{NANSWAP_API}/get-limits",
        params={"from": from_asset.upper(), "to": to_asset.upper()},
        headers=_headers(),
        timeout=30,
    )
    data = r.json()
    if r.status_code >= 400 or "error" in data:
        raise TopupError(f"NanSwap limits failed: {data}")
    return data


def create_order(
    from_asset: str, amount: float, receive_addr: str, to_asset: str = "XNO"
) -> dict:
    """Create a swap order. Returns deposit address + order id.

    The agent then sends `amount` of from_asset to the returned deposit
    address; NanSwap delivers XNO to receive_addr.
    """
    if not _key():
        raise TopupError(
            "NANSWAP_API_KEY not set. Get a free key at https://nanswap.com "
            "(or swap manually: ChangeNOW, SimpleSwap, Trocador — send XNO "
            f"to {receive_addr})"
        )
    r = requests.post(
        f"{NANSWAP_API}/create-order",
        json={
            "from": from_asset.upper(),
            "to": to_asset.upper(),
            "amount": amount,
            "toAddress": receive_addr,
        },
        headers=_headers(),
        timeout=30,
    )
    data = r.json()
    if r.status_code >= 400 or "error" in data:
        raise TopupError(f"NanSwap create-order failed: {data}")
    return data
