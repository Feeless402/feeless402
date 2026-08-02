"""Offline tests for the money-handling paths. No network, no real funds."""

import base64
import copy
import json
import os
import sys
import types

import nanopy
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nano_pay import raw_to_xno, xno_to_raw
from nano_pay.verify import PaymentInvalid, _seen_previous, verify_block
from nano_pay.x402 import (
    PriceCapExceeded,
    X402Error,
    build_payment_header,
    offer_amount_raw,
    offer_pay_to,
    pick_nano_offer,
)

SEED = "7" * 64
NET = nanopy.Network()


def make_account(index=0, frontier="A" * 64, raw_bal=10**30):
    acct = nanopy.Account(sk=nanopy.deterministic_key(SEED, index))
    acct.frontier = frontier
    acct.raw_bal = raw_bal
    acct.rep = nanopy.Account(
        addr="nano_1center16ci77qw5w69ww8sy4i4bfmgfhr81ydzpurm91cauj11jn6y3uc5y"
    )
    return acct


def signed_send(payer, dest_addr, raw_amt):
    return payer.send(
        nanopy.Account(addr=dest_addr), raw_amt, work="0000000000000000"
    )


class FakeRPC:
    """account_info stub reflecting the pre-send chain state."""

    def __init__(self, frontier, balance):
        self.frontier = frontier
        self.balance = balance

    def account_info(self, addr):
        return {
            "frontier": self.frontier,
            "balance": str(self.balance),
            "representative": "nano_1center16ci77qw5w69ww8sy4i4bfmgfhr81ydzpurm91cauj11jn6y3uc5y",
        }


AMOUNT = xno_to_raw("0.0001")


@pytest.fixture(autouse=True)
def clean_seen():
    _seen_previous.clear()
    yield
    _seen_previous.clear()


@pytest.fixture
def merchant():
    return make_account(1).addr


@pytest.fixture
def payment(merchant):
    payer = make_account(0)
    frontier, balance = payer.frontier, payer.raw_bal
    blk = signed_send(payer, merchant, AMOUNT)
    return blk.dict_, FakeRPC(frontier, balance)


def test_valid_payment_accepted(payment, merchant):
    block, rpc = payment
    payer_addr = verify_block(block, AMOUNT, merchant, rpc)
    assert payer_addr == make_account(0).addr


def test_wrong_amount_rejected(payment, merchant):
    block, rpc = payment
    with pytest.raises(PaymentInvalid, match="balance decrease"):
        verify_block(block, AMOUNT * 2, merchant, rpc)


def test_wrong_destination_rejected(payment):
    block, rpc = payment
    other = make_account(2).addr
    with pytest.raises(PaymentInvalid, match="does not pay this server"):
        verify_block(block, AMOUNT, other, rpc)


def test_forged_signature_rejected(payment, merchant):
    block, rpc = payment
    forged = copy.deepcopy(block)
    sig = bytearray(bytes.fromhex(forged["signature"]))
    sig[0] ^= 0xFF
    forged["signature"] = bytes(sig).hex()
    with pytest.raises(PaymentInvalid, match="signature"):
        verify_block(forged, AMOUNT, merchant, rpc)


def test_tampered_balance_rejected(payment, merchant):
    block, rpc = payment
    tampered = copy.deepcopy(block)
    tampered["balance"] = str(int(tampered["balance"]) - 1)  # steal 1 raw more
    with pytest.raises(PaymentInvalid):
        verify_block(tampered, AMOUNT + 1, merchant, rpc)  # sig no longer valid


def test_stale_frontier_rejected(payment, merchant):
    block, rpc = payment
    rpc.frontier = "B" * 64  # chain moved on
    with pytest.raises(PaymentInvalid, match="frontier"):
        verify_block(block, AMOUNT, merchant, rpc)


def test_replay_rejected(payment, merchant):
    block, rpc = payment
    verify_block(block, AMOUNT, merchant, rpc)
    _seen_previous[block["previous"].upper()] = 9999999999
    with pytest.raises(PaymentInvalid, match="already accepted"):
        verify_block(block, AMOUNT, merchant, rpc)


# ---------- quote parsing: both dialects ----------

NANOGPT_QUOTE = {
    "error": {"code": "insufficient_quota"},
    "payment": {
        "version": 1,
        "paymentId": "pay_abc",
        "accepted": [
            {"scheme": "nano", "protocolScheme": "nano",
             "network": "nano-mainnet", "amount": "100",
             "payTo": "nano_1manual", "paymentId": "pay_abc"},
            {"scheme": "nano-exact", "protocolScheme": "exact",
             "network": "nano:mainnet", "amount": "100", "asset": "XNO",
             "payTo": "nano_3exact11111111111111111111111111111111111111111111111111111111",
             "paymentId": "pay_def"},
            {"scheme": "x402-exact", "network": "base", "amount": "1000",
             "payTo": "0xdead"},
        ],
    },
}

X402NANO_QUOTE = {
    "x402Version": 2,
    "accepts": [
        {"scheme": "exact", "network": "base", "asset": "USDC",
         "amount": "1000", "payTo": "0xdead"},
        {"scheme": "exact", "network": "nano:mainnet", "asset": "XNO",
         "amount": "100",
         "payTo": "nano_3exact11111111111111111111111111111111111111111111111111111111"},
    ],
}


def test_pick_prefers_exact_scheme_nanogpt():
    offer = pick_nano_offer(NANOGPT_QUOTE)
    assert offer["protocolScheme"] == "exact"
    assert offer["network"] == "nano:mainnet"


def test_pick_x402nano_dialect():
    offer = pick_nano_offer(X402NANO_QUOTE)
    assert offer["network"] == "nano:mainnet"
    assert offer_amount_raw(offer) == 100
    assert offer_pay_to(offer).startswith("nano_3exact")


def test_no_nano_offer_raises():
    with pytest.raises(X402Error, match="no Nano payment option"):
        pick_nano_offer({"accepts": [{"scheme": "exact", "network": "base"}]})


def test_header_nanogpt_dialect_shape():
    offer = pick_nano_offer(NANOGPT_QUOTE)
    hdr = build_payment_header(
        NANOGPT_QUOTE, offer, {"type": "state", "link": "AB"}, "nano_3dest"
    )
    payload = json.loads(base64.b64decode(hdr))
    assert payload["x402Version"] == 1
    assert payload["scheme"] == "exact"
    assert payload["payload"]["paymentId"] == "pay_def"
    assert payload["payload"]["block"]["link_as_account"] == "nano_3dest"


def test_header_v2_dialect_shape():
    offer = pick_nano_offer(X402NANO_QUOTE)
    hdr = build_payment_header(
        X402NANO_QUOTE, offer, {"type": "state", "link": "AB"}, "nano_3dest"
    )
    payload = json.loads(base64.b64decode(hdr))
    assert payload["x402Version"] == 2
    assert payload["accepted"] == offer


# ---------- price cap ----------

def test_price_cap_math():
    assert xno_to_raw("0.05") == 5 * 10**28
    assert raw_to_xno(5 * 10**28) == "0.05"
    quote_amount = xno_to_raw("0.06")
    cap = xno_to_raw("0.05")
    assert quote_amount > cap  # request_with_payment refuses in this case
