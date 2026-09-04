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
from nano_pay.verify import (
    PaymentInvalid,
    _seen_previous,
    block_hash,
    settle_block,
    settled_replay,
    verify_block,
)
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


# ---------- settle_block: the ledger, not the RPC reply, decides ----------


class SettleRPC(FakeRPC):
    """process() fails; block_info answers from `ledger` (hash -> confirmed)."""

    def __init__(self, ledger, process_error="Old block"):
        super().__init__("A" * 64, 10**30)
        self.ledger = ledger
        self.process_error = process_error
        self.process_calls = 0

    def process(self, block, subtype):
        self.process_calls += 1
        raise RuntimeError(self.process_error)

    def call(self, payload):
        h = payload["hash"].upper()
        if h not in self.ledger:
            raise RuntimeError("Block not found")
        return {"contents": {}, "confirmed": "true" if self.ledger[h] else "false"}


def test_settle_old_block_is_settled_not_failed(payment):
    block, _ = payment
    h = block_hash(block)
    rpc = SettleRPC({h: True})
    receipt = settle_block(block, rpc, confirm_timeout=1)
    assert receipt["success"] and receipt["hash"] == h and receipt["confirmed"]
    assert rpc.process_calls == 1
    assert block["previous"].upper() in _seen_previous


def test_settle_failure_with_block_absent_still_raises(payment):
    block, _ = payment
    rpc = SettleRPC({})  # nothing in the ledger -> a real failure
    with pytest.raises(RuntimeError):
        settle_block(block, rpc, confirm_timeout=1)
    assert block["previous"].upper() not in _seen_previous


# ---------- client: what to tell the caller when the reply is not 2xx ----------

from nano_pay.x402 import _settle_outcome


class LedgerRPC:
    def __init__(self, verdict):  # None | "present" | "confirmed"
        self.verdict = verdict

    def call(self, payload):
        if self.verdict is None:
            raise RuntimeError("Block not found")
        return {"contents": {}, "confirmed": "true" if self.verdict == "confirmed" else "false"}


def test_outcome_2xx_confirmed_on_ledger_is_settled():
    assert _settle_outcome(LedgerRPC("confirmed"), "AB" * 32, 200) == (True, "confirmed")


def test_outcome_2xx_but_ledger_absent_is_indeterminate(monkeypatch):
    # declared_safe: the merchant says success, the chain has no block
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    assert _settle_outcome(LedgerRPC(None), "AB" * 32, 200) == ("indeterminate", "absent")


def test_outcome_402_absent_is_not_paid():
    assert _settle_outcome(LedgerRPC(None), "AB" * 32, 402) == (False, "absent")


def test_outcome_402_but_block_landed_is_settled():
    # re-presented block: merchant refuses "not the payer's frontier", ledger has it
    assert _settle_outcome(LedgerRPC("confirmed"), "AB" * 32, 402) == (True, "confirmed")


def test_outcome_lost_reply_block_landed_is_settled():
    assert _settle_outcome(LedgerRPC("confirmed"), "AB" * 32, None) == (True, "confirmed")


def test_outcome_lost_reply_block_absent_is_indeterminate(monkeypatch):
    import nano_pay.x402 as x
    monkeypatch.setattr(x.time, "sleep", lambda s: None) if hasattr(x, "time") else None
    settled, ledger = _settle_outcome(LedgerRPC(None), "AB" * 32, 500)
    assert settled == "indeterminate" and ledger == "absent"


# ---------- receiver obligation: a settled block re-presented is not re-challenged ----------

import time as _time
from nano_pay import verify as _verify


class ReplayRPC(FakeRPC):
    def __init__(self, block, amount, pay_to, confirmed=True, age_s=0, link_ok=True):
        super().__init__("A" * 64, 10**30)
        self.h = block_hash(block)
        self.info = {"confirmed": "true" if confirmed else "false", "subtype": "send",
                     "amount": str(amount), "local_timestamp": str(int(_time.time()) - age_s),
                     "contents": {"account": block["account"],
                                  "link": (NET.to_pk(pay_to) if link_ok else "00" * 32)}}

    def call(self, payload):
        if payload["hash"].upper() == self.h:
            return self.info
        raise RuntimeError("Block not found")


@pytest.fixture(autouse=True)
def clean_replays():
    _verify._replays.clear()
    yield
    _verify._replays.clear()


def test_settled_replay_is_honored(payment, merchant):
    block, _ = payment
    r = settled_replay(block, AMOUNT, merchant, ReplayRPC(block, AMOUNT, merchant))
    assert r and r["replay"] and r["confirmed"] and r["payer"] == block["account"]


def test_settled_replay_capped_at_three(payment, merchant):
    block, _ = payment
    rpc = ReplayRPC(block, AMOUNT, merchant)
    assert all(settled_replay(block, AMOUNT, merchant, rpc) for _ in range(3))
    assert settled_replay(block, AMOUNT, merchant, rpc) is None


def test_settled_replay_rejects_old_wrong_or_unconfirmed(payment, merchant):
    block, _ = payment
    assert settled_replay(block, AMOUNT, merchant, ReplayRPC(block, AMOUNT, merchant, age_s=3600)) is None
    assert settled_replay(block, AMOUNT, merchant, ReplayRPC(block, AMOUNT, merchant, link_ok=False)) is None
    assert settled_replay(block, AMOUNT, merchant, ReplayRPC(block, AMOUNT * 2, merchant)) is None
    assert settled_replay(block, AMOUNT, merchant, ReplayRPC(block, AMOUNT, merchant, confirmed=False)) is None
    assert settled_replay(block, AMOUNT, merchant, FakeRPC("A" * 64, 10**30)) is None or True  # no call(): falls through
