"""Retry safety (GHSA-cx37-j5vc-c967, reported privately against 0.2.8).

1. A client retry after a lost reply must RE-PRESENT the block it already signed, never sign a second one
   (that is a double charge) — both inside one call and in a fresh call after a crash.
2. A retry a little after 15 minutes must still be honored (was: paid-but-not-served).
3. An observer who replays a public, settled block must not be able to use up the payer's own retries.
No network, no real funds.
"""
import base64
import json
import os
import sys
import time

import nanopy
import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nano_pay import x402
from nano_pay import verify as V
from nano_pay.verify import block_hash, settled_replay

SEED = "7" * 64
PAY_TO_ACCT = nanopy.Account(sk=nanopy.deterministic_key(SEED, 1))
PAY_TO = PAY_TO_ACCT.addr
QUOTE = {"x402Version": 2, "accepts": [{"scheme": "exact", "network": "nano:mainnet", "asset": "XNO",
                                         "amount": "100", "payTo": PAY_TO}]}


class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeWallet:
    """Just enough wallet: signs real Nano send blocks, counts how many it signed."""

    def __init__(self, tmp):
        self.path = tmp / "wallet.json"
        self.signed = 0
        self.acct = nanopy.Account(sk=nanopy.deterministic_key(SEED, 0))
        self.acct.frontier = "A" * 64
        self.acct.raw_bal = 10**30
        self.acct.rep = nanopy.Account(addr="nano_1center16ci77qw5w69ww8sy4i4bfmgfhr81ydzpurm91cauj11jn6y3uc5y")

    def account(self):
        return self.acct

    @property
    def address(self):
        return self.acct.addr

    def build_payment_block(self, rpc, to_addr, raw_amt):
        self.signed += 1
        a = nanopy.Account(sk=nanopy.deterministic_key(SEED, 0))
        a.frontier, a.raw_bal, a.rep = self.acct.frontier, self.acct.raw_bal, self.acct.rep
        blk = a.send(nanopy.Account(addr=to_addr), raw_amt, work="0000000000000000")
        return blk.dict_, blk.hash_, "A" * 64

    def payment_succeeded(self, *a, **k):
        pass

    def payment_failed(self, *a, **k):
        pass


@pytest.fixture
def net(monkeypatch):
    """Scripted merchant: a list of callables, each returns a Resp or raises."""
    calls, script = [], []

    def fake_request(method, url, headers=None, **kw):
        calls.append(dict(headers or {}))
        step = script.pop(0)
        return step(headers or {})

    monkeypatch.setattr(x402.requests, "request", fake_request)
    monkeypatch.setattr(x402, "_ledger_verdict", lambda rpc, h, wait=0.0: "confirmed")
    monkeypatch.setattr(x402.time, "sleep", lambda s: None, raising=False)
    return calls, script


def quote(h):
    return Resp(402, QUOTE)


def lost(h):
    raise requests.ConnectionError("reply lost")


def served(h):
    return Resp(200, {"ok": True})


def paid_hdr(h):
    return h.get("PAYMENT-SIGNATURE") or h.get("X-PAYMENT")


# --- 1a: a retry inside the same call re-presents the same block ---------------------------------------------------
def test_retry_in_same_call_re_presents_same_block(tmp_path, net):
    calls, script = net
    w = FakeWallet(tmp_path)
    script += [quote, lost, served]
    r, rec = x402.request_with_payment("GET", "https://m.example/premium", w, rpc=None, max_raw=1000)
    assert r.status_code == 200
    assert w.signed == 1, "signed a second block on retry: double charge"
    assert paid_hdr(calls[1]) == paid_hdr(calls[2]), "retry did not re-present the same payment"


# --- 1b: a fresh call after a crash re-presents the journaled block ------------------------------------------------
def test_new_call_after_crash_re_presents_journaled_block(tmp_path, net):
    calls, script = net
    w = FakeWallet(tmp_path)
    script += [quote, lost, lost, lost]
    with pytest.raises(x402.PaidRequestFailed):
        x402.request_with_payment("GET", "https://m.example/premium", w, rpc=None, max_raw=1000)
    first = paid_hdr(calls[1])
    script += [quote, served]
    r, rec = x402.request_with_payment("GET", "https://m.example/premium", w, rpc=None, max_raw=1000)
    assert r.status_code == 200
    assert w.signed == 1, "a new call after a crash paid again: double charge"
    assert paid_hdr(calls[-1]) == first
    assert calls[-1].get("X-PAYMENT-PROOF"), "re-presentation carries no payer proof"


def test_served_payment_is_not_re_presented_for_the_next_purchase(tmp_path, net):
    calls, script = net
    w = FakeWallet(tmp_path)
    script += [quote, served, quote, served]
    x402.request_with_payment("GET", "https://m.example/premium", w, rpc=None, max_raw=1000)
    w.acct.frontier = "B" * 64            # the first payment moved the frontier
    x402.request_with_payment("GET", "https://m.example/premium", w, rpc=None, max_raw=1000)
    assert w.signed == 2, "a second, separate purchase must be a second payment"


# --- server side ---------------------------------------------------------------------------------------------------
class LedgerRPC:
    def __init__(self, blk, age_s):
        self.blk, self.age = blk, age_s

    def call(self, req):
        return {"confirmed": "true", "subtype": "send", "amount": "100",
                "local_timestamp": str(int(time.time() - self.age)),
                "contents": {"link": PAY_TO_ACCT.pk.upper(), "account": self.blk["account"]}}


def settled_block():
    a = nanopy.Account(sk=nanopy.deterministic_key(SEED, 0))
    a.frontier, a.raw_bal = "A" * 64, 10**30
    a.rep = nanopy.Account(addr="nano_1center16ci77qw5w69ww8sy4i4bfmgfhr81ydzpurm91cauj11jn6y3uc5y")
    return a.send(PAY_TO_ACCT, 100, work="0000000000000000").dict_, a


@pytest.fixture(autouse=True)
def clean_replays():
    V._replays.clear()
    yield
    V._replays.clear()


def test_retry_at_16_minutes_is_still_honored():
    blk, _ = settled_block()
    assert settled_replay(blk, 100, PAY_TO, LedgerRPC(blk, 16 * 60)), "paid but not served at 16 minutes"


def test_observer_cannot_exhaust_the_payers_retries():
    blk, payer = settled_block()
    rpc = LedgerRPC(blk, 60)
    for _ in range(10):                   # an observer replays the public block from its own address
        settled_replay(blk, 100, PAY_TO, rpc, requester="203.0.113.9")
    proof = x402.replay_proof(payer, block_hash(blk), "GET", "/premium")
    assert settled_replay(blk, 100, PAY_TO, rpc, requester="198.51.100.4", proof=proof,
                          method="GET", path="/premium"), "the payer's own retry was refused"


def test_forged_proof_is_not_accepted_as_the_payers():
    blk, payer = settled_block()
    rpc = LedgerRPC(blk, 60)
    stranger = nanopy.Account(sk=nanopy.deterministic_key(SEED, 9))
    bad = x402.replay_proof(stranger, block_hash(blk), "GET", "/premium")
    for _ in range(10):
        settled_replay(blk, 100, PAY_TO, rpc, requester="203.0.113.9", proof=bad, method="GET", path="/premium")
    # the forged proof counted as an anonymous replay from the observer's address, and ran out there
    assert settled_replay(blk, 100, PAY_TO, rpc, requester="203.0.113.9", proof=bad, method="GET", path="/premium") is None
