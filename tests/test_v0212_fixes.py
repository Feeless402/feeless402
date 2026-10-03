"""0.2.12: fixes for the issues reported in PRs #10-#18, written fresh.

Each test names the PR whose report it covers. Every one fails on 0.2.11.
"""
import json
import time

import pytest

from nano_pay import x402
from nano_pay import receipt as R
from nano_pay.x402 import _ledger_verdict, _settle_outcome
UNREACHABLE = getattr(x402, "UNREACHABLE", "unreachable")

from test_retry_safety import FakeWallet, QUOTE, Resp, lost, net, paid_hdr, quote  # noqa: F401  (fixture)

H = "AB" * 32


class DownRPC:
    """Every node fails: the ledger cannot be asked."""
    def call(self, payload):
        raise RuntimeError("all RPC nodes failed, last error: timeout")


class AbsentRPC:
    """The node answers: no such block."""
    def call(self, payload):
        raise RuntimeError("Block not found")


class Clock:
    def __init__(self, monkeypatch):
        self.t = 1000.0
        monkeypatch.setattr(time, "time", lambda: self.t)
        monkeypatch.setattr(time, "sleep", lambda s: setattr(self, "t", self.t + s))


# --- a ledger we cannot ask is never "not paid" (PRs #10, #13, #18) ------------------------------------------------
def test_refusal_with_unreachable_ledger_is_indeterminate(monkeypatch):
    Clock(monkeypatch)
    assert _settle_outcome(DownRPC(), H, 402) == ("indeterminate", UNREACHABLE)


def test_success_with_unreachable_ledger_is_indeterminate(monkeypatch):
    Clock(monkeypatch)
    assert _settle_outcome(DownRPC(), H, 200) == ("indeterminate", UNREACHABLE)


def test_refusal_with_ledger_answering_absent_is_still_not_paid(monkeypatch):
    Clock(monkeypatch)
    assert _settle_outcome(AbsentRPC(), H, 402) == (False, "absent")


def test_refusal_waits_for_a_block_that_lands_at_second_4(monkeypatch):
    clock = Clock(monkeypatch)
    start = clock.t

    class LateRPC:
        def call(self, payload):
            if clock.t - start < 4:
                raise RuntimeError("Block not found")
            return {"contents": {}, "confirmed": "true"}

    assert _settle_outcome(LateRPC(), H, 402) == (True, "confirmed")


def test_a_block_once_seen_is_not_unseen_by_a_later_error(monkeypatch):
    clock = Clock(monkeypatch)
    n = {"i": 0}

    class FlakyRPC:
        def call(self, payload):
            n["i"] += 1
            if n["i"] == 1:
                return {"contents": {}, "confirmed": "false"}
            raise RuntimeError("all RPC nodes failed")

    assert _ledger_verdict(FlakyRPC(), H, wait=2.0) == "present"


def _journal_one_lost_payment(w, script):
    script += [quote, lost, lost, lost]
    with pytest.raises(x402.PaidRequestFailed):
        x402.request_with_payment("GET", "https://m.example/premium", w, rpc=None, max_raw=1000)


def test_refused_re_presentation_with_unreachable_ledger_does_not_pay_again(tmp_path, net, monkeypatch):
    calls, script = net
    w = FakeWallet(tmp_path)
    _journal_one_lost_payment(w, script)
    monkeypatch.setattr(x402, "_ledger_verdict", lambda rpc, h, wait=0.0: UNREACHABLE)
    script += [quote, lambda h: Resp(402, QUOTE)]
    with pytest.raises(x402.PaidRequestFailed) as e:
        x402.request_with_payment("GET", "https://m.example/premium", w, rpc=None, max_raw=1000)
    assert w.signed == 1, "signed a second payment while the ledger could not be checked"
    assert e.value.receipt["settled"] == "indeterminate"


def test_refused_re_presentation_with_ledger_absent_pays_fresh(tmp_path, net, monkeypatch):
    calls, script = net
    w = FakeWallet(tmp_path)
    _journal_one_lost_payment(w, script)
    monkeypatch.setattr(x402, "_ledger_verdict", lambda rpc, h, wait=0.0: None)
    script += [quote, lambda h: Resp(402, QUOTE), quote, lambda h: Resp(200, {"ok": True})]
    r, rec = x402.request_with_payment("GET", "https://m.example/premium", w, rpc=None, max_raw=1000)
    assert r.status_code == 200
    assert w.signed == 2, "the earlier block never landed, so a fresh payment is right"


# --- the receipt checker (PR #12) -----------------------------------------------------------------------------------
def test_receipt_unreachable_node_is_not_not_found(monkeypatch):
    monkeypatch.setattr(R, "RPC", lambda urls, timeout: DownRPC())
    with pytest.raises(R.Unreachable):
        R.verify(H, 100, "nano_x", "https://node.example")


def test_receipt_absent_block_is_not_found(monkeypatch):
    monkeypatch.setattr(R, "RPC", lambda urls, timeout: AbsentRPC())
    with pytest.raises(R.NotFound):
        R.verify(H, 100, "nano_x", "https://node.example")


# --- the journal fails closed (PRs #15, #16) ------------------------------------------------------------------------
def test_corrupt_journal_refuses_to_pay(tmp_path, net):
    calls, script = net
    w = FakeWallet(tmp_path)
    (tmp_path / "pending-payments.json").write_text("{not json")
    script += [quote]
    with pytest.raises(x402.X402Error):
        x402.request_with_payment("GET", "https://m.example/premium", w, rpc=None, max_raw=1000)
    assert w.signed == 0 and len(calls) == 1, "paid with a journal it could not read"


def test_unwritable_journal_refuses_to_send(tmp_path, net):
    calls, script = net
    w = FakeWallet(tmp_path)
    (tmp_path / "pending-payments.tmp").mkdir()        # the atomic write's temp file cannot be created
    script += [quote]
    with pytest.raises(x402.X402Error):
        x402.request_with_payment("GET", "https://m.example/premium", w, rpc=None, max_raw=1000)
    assert len(calls) == 1, "sent a payment it could not record"


# --- the journal is bound to the request's contents (PR #17) ---------------------------------------------------------
def test_different_bodies_do_not_share_a_payment(tmp_path, net):
    calls, script = net
    w = FakeWallet(tmp_path)
    script += [quote, lost, lost, lost]
    with pytest.raises(x402.PaidRequestFailed):
        x402.request_with_payment("POST", "https://m.example/premium", w, rpc=None, max_raw=1000,
                                  json={"order": 1})
    first = paid_hdr(calls[1])
    w.acct.frontier = "B" * 64
    script += [quote, lambda h: Resp(200, {"ok": True})]
    x402.request_with_payment("POST", "https://m.example/premium", w, rpc=None, max_raw=1000, json={"order": 2})
    assert paid_hdr(calls[-1]) != first, "order 2 re-presented order 1's payment"
    assert w.signed == 2


def test_same_body_still_re_presents(tmp_path, net):
    calls, script = net
    w = FakeWallet(tmp_path)
    script += [quote, lost, lost, lost]
    with pytest.raises(x402.PaidRequestFailed):
        x402.request_with_payment("POST", "https://m.example/premium", w, rpc=None, max_raw=1000,
                                  json={"b": 2, "a": 1})
    script += [quote, lambda h: Resp(200, {"ok": True})]
    x402.request_with_payment("POST", "https://m.example/premium", w, rpc=None, max_raw=1000, json={"a": 1, "b": 2})
    assert w.signed == 1, "the same request (keys in a different order) paid twice"


def test_plain_get_key_is_unchanged_from_0211():
    assert x402._journal_key("GET", "u", "p", 1, x402._request_fingerprint({})) == x402._journal_key("GET", "u", "p", 1)


# --- blank settings (PR #14) -----------------------------------------------------------------------------------------
def test_blank_setting_gets_the_default(monkeypatch):
    from nano_pay import server
    monkeypatch.setenv("F402_FAUCET_PER_IP_PER_DAY", "")
    assert server._env("F402_FAUCET_PER_IP_PER_DAY", "3") == "3"
    monkeypatch.setenv("F402_FAUCET_PER_IP_PER_DAY", "10")
    assert server._env("F402_FAUCET_PER_IP_PER_DAY", "3") == "10"


def test_blank_topup_stays_off(monkeypatch):
    from nano_pay import server
    monkeypatch.setenv("F402_FAUCET_TOPUP_XNO", "")
    assert server._env("F402_FAUCET_TOPUP_XNO", "0.0045", if_blank="0") == "0"
    monkeypatch.delenv("F402_FAUCET_TOPUP_XNO")
    assert server._env("F402_FAUCET_TOPUP_XNO", "0.0045", if_blank="0") == "0.0045"
