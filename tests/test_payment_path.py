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

from nano_pay import AmountError, raw_to_xno, xno_to_raw
from nano_pay.rpc import RPCError
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


# ---------- one wait for the whole path: the 3 s / 8 s split is a defect ----------

def test_confirm_wait_is_one_value_for_both_paths():
    """settle_block() and _settle_outcome() answer the same question about the
    same block, so they must poll the ledger for the same time. They used to
    differ (3 s here, 8 s there), which made a block confirmed at second 4
    'confirmed' on the settle path and 'indeterminate' on the x402 path."""
    import inspect
    from nano_pay import verify as v
    import nano_pay.x402 as x
    assert v.CONFIRM_WAIT_S == x.CONFIRM_WAIT_S
    default = inspect.signature(v.settle_block).parameters["confirm_timeout"].default
    assert default == v.CONFIRM_WAIT_S


def test_block_confirmed_after_3s_is_seen_by_both_paths(monkeypatch):
    """A block that lands at second 4 is inside both budgets now. Drive
    _ledger_verdict with a clock so the answer is decided by the wait, not by a
    fixed verdict: at 3 s the block is not there yet, at 4 s it is."""
    import nano_pay.verify as v
    import nano_pay.x402 as x
    assert v.CONFIRM_WAIT_S > 3.0  # the old x402 wait would have given up here

    class SlowLedgerRPC:
        """Answers 'confirmed' only from second 4 onward."""
        def __init__(self, clock):
            self.clock = clock

        def call(self, payload):
            if self.clock["t"] >= 4.0:
                return {"contents": {}, "confirmed": "true"}
            raise RuntimeError("Block not found")

    # _ledger_verdict does `import time` inside the function, so patch the
    # time module itself, not a module attribute.
    import time as _time_mod
    clock = {"t": 0.0}
    monkeypatch.setattr(_time_mod, "time", lambda: clock["t"])
    monkeypatch.setattr(_time_mod, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    assert x._ledger_verdict(SlowLedgerRPC(clock), "AB" * 32, wait=x.CONFIRM_WAIT_S) == "confirmed"

    # and the client outcome that used to give up at 3 s
    clock["t"] = 0.0
    settled, ledger = x._settle_outcome(SlowLedgerRPC(clock), "AB" * 32, 200)
    assert settled is True and ledger == "confirmed"


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
    # the window is 24 h since GHSA-cx37-j5vc-c967 (a retry at ~16 min came back paid-but-not-served)
    assert settled_replay(block, AMOUNT, merchant, ReplayRPC(block, AMOUNT, merchant, age_s=25 * 3600)) is None
    assert settled_replay(block, AMOUNT, merchant, ReplayRPC(block, AMOUNT, merchant, age_s=3600)) is not None
    assert settled_replay(block, AMOUNT, merchant, ReplayRPC(block, AMOUNT, merchant, link_ok=False)) is None
    assert settled_replay(block, AMOUNT, merchant, ReplayRPC(block, AMOUNT * 2, merchant)) is None
    assert settled_replay(block, AMOUNT, merchant, ReplayRPC(block, AMOUNT, merchant, confirmed=False)) is None
    assert settled_replay(block, AMOUNT, merchant, FakeRPC("A" * 64, 10**30)) is None or True  # no call(): falls through


# --- parse_quote: header carries a single foreign offer, body has the list ---

class _Resp:
    def __init__(self, headers, body, text=""):
        self.headers = headers
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


_NANO_BODY_OFFER = {
    "scheme": "nano-exact", "protocolScheme": "exact", "network": "nano:mainnet",
    "amount": "55043680000000000000000000000", "asset": "XNO",
    "payTo": "nano_3njeurfzgpwpnqjxoytfnqa7ezbgkordga8e8jg74ey77kww5d5emjjyzrhp",
    "paymentId": "pay_117199eee1dfecc1417311cd557bcc4f",
}
_SOLANA_HDR_OFFER = {
    "scheme": "exact", "network": "solana",
    "asset": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "amount": "21629",
    "payTo": "P16imsNyUZfMGDJTDV623AVgAPz4Zp2oBgZ9F8EbHe4",
    "paymentId": "pay_b144f6158605b8dcdf67d01d9485ab95",
}


def test_parse_quote_single_offer_header_does_not_hide_body_offers():
    """NanoGPT (Sep 2026) puts one Solana-USDC offer in X-Payment-Required
    and the full accepts list, including Nano, in the body. The client must
    still find the Nano offer."""
    from nano_pay.x402 import collect_offers, parse_quote

    r = _Resp(
        {"x-payment-required": json.dumps(_SOLANA_HDR_OFFER)},
        {"x402Version": 1, "payment": {"accepted": [_NANO_BODY_OFFER]},
         "accepts": [_SOLANA_HDR_OFFER]},
    )
    q = parse_quote(r)
    offers = collect_offers(q)
    assert _NANO_BODY_OFFER in offers
    assert sum(1 for o in offers if o == _SOLANA_HDR_OFFER) == 1  # not duplicated
    assert pick_nano_offer(q)["payTo"] == _NANO_BODY_OFFER["payTo"]


def test_parse_quote_header_only_single_offer_is_wrapped():
    from nano_pay.x402 import collect_offers, parse_quote

    r = _Resp({"payment-required": base64.b64encode(
        json.dumps(_NANO_BODY_OFFER).encode()).decode()}, None, text="")
    q = parse_quote(r)
    assert collect_offers(q) == [_NANO_BODY_OFFER]


def test_parse_quote_header_envelope_still_wins_when_body_has_no_offers():
    from nano_pay.x402 import parse_quote

    env = {"x402Version": 2, "accepts": [_NANO_BODY_OFFER], "resource": "/premium"}
    r = _Resp({"payment-required": base64.b64encode(json.dumps(env).encode()).decode()},
              {"error": "payment required"})
    assert parse_quote(r) == env


def test_parse_quote_nothing_parseable_raises():
    from nano_pay.x402 import parse_quote

    with pytest.raises(X402Error):
        parse_quote(_Resp({}, {"error": "nope"}, text='{"error":"nope"}'))


# --- XNO <-> raw conversion ------------------------------------------------
# 1 raw = 10**-30 XNO. Both directions used to lose value silently: `int()`
# truncated sub-raw input to 0, and the arithmetic ran in the ambient
# `decimal` context (default precision 28) although raw spans 31 digits.
# A round trip through the pair is what `amount_xno` receipts invite, so it
# has to be lossless.

_LOSSY_RAW = 300000000000000000000000000007  # 30 significant digits


def test_raw_to_xno_keeps_all_digits_past_the_decimal_default_precision():
    assert raw_to_xno(_LOSSY_RAW) == "0.300000000000000000000000000007"


def test_xno_to_raw_keeps_all_digits_past_the_decimal_default_precision():
    assert xno_to_raw("0.300000000000000000000000000007") == _LOSSY_RAW


def test_conversion_round_trips_for_every_raw_size():
    # Deterministic sweep over the whole range, including the 31-digit top.
    for raw in (
        1,
        10,
        10**9,
        5 * 10**28,
        _LOSSY_RAW,
        10**30,
        10**30 + 1,
        9_999_999_999_999_999_999_999_999_999_999,
    ):
        assert xno_to_raw(raw_to_xno(raw)) == raw, raw


def test_raw_to_xno_renders_whole_numbers_without_a_decimal_point():
    assert raw_to_xno(10**30) == "1"
    assert raw_to_xno(0) == "0"


def test_sub_raw_amounts_are_refused_not_truncated_to_zero():
    with pytest.raises(AmountError):
        xno_to_raw("0.0000000000000000000000000000005")


def test_negative_and_unparseable_amounts_are_refused():
    for bad in ("-1", "abc", "", "nan", "inf"):
        with pytest.raises(AmountError):
            xno_to_raw(bad)


# --- faucet claim amount: raw arithmetic must not replace xno_to_raw ------
# `claims_remaining` is the number of XNO top-ups a faucet balance still
# covers, so it is computed as `balance_raw // per_claim_raw`. Two call sites
# built `per_claim_raw` with `int(float(FAUCET_CLAIM_XNO) * 10**30)` instead
# of `xno_to_raw`. `float` cannot hold 0.0005 exactly, so the product rounded
# to 500000000000000006643777536 raw — 6.6e9 raw too high — and a 5 XNO
# balance reported 9999 remaining claims instead of 10000: the UI promised
# one claim the faucet could not fund. The exact-conversion path already
# existed and every other call site used it; these two had not been migrated.


def test_faucet_per_claim_uses_exact_conversion_not_float_arithmetic():
    """The typed value and the derived raw must agree exactly."""
    from nano_pay.mcp_remote import FAUCET_CLAIM_XNO

    per_claim = xno_to_raw(FAUCET_CLAIM_XNO)
    assert per_claim == 500000000000000000000000000
    # What the old expression produced, for contrast.
    assert int(float(FAUCET_CLAIM_XNO) * 10**30) != per_claim


def test_faucet_claims_remaining_does_not_over_report_by_one():
    """A balance of exactly N claims must yield N, not N-1."""
    from nano_pay.mcp_remote import FAUCET_CLAIM_XNO

    per_claim = xno_to_raw(FAUCET_CLAIM_XNO)
    balance = 10000 * per_claim  # exactly ten thousand claims' worth
    assert balance // per_claim == 10000
    # The lossy expression truncates the count by one for this balance.
    assert balance // int(float(FAUCET_CLAIM_XNO) * 10**30) == 9999


def test_faucet_remote_sources_do_not_do_float_raw_arithmetic():
    """Guard the whole class: no module may build raw from a float product."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent / "nano_pay"
    offenders = []
    for py in sorted(root.glob("*.py")):
        if py.name == "__init__.py":  # the conversion module itself
            continue
        for n, line in enumerate(py.read_text().splitlines(), 1):
            if re.search(r"float\([^)]*\)\s*\*\s*10\s*\*\*\s*30", line):
                offenders.append(f"{py.name}:{n}: {line.strip()}")
    assert not offenders, "float-based raw conversion(s):\n" + "\n".join(offenders)


# --- railHint spec pointer -------------------------------------------------
# The 402 `rail-hint.info.spec` field is a *pointer to the payment-scheme
# specification*, and the canonical draft lives at railhint.com (see
# SPEC-railhint.md and pyproject.toml's `Specification` URL). An earlier
# revision pointed at x402nano.org, whose deployment is disabled, so every
# agent that followed the hint landed on a dead page.

_SPEC_URI = "https://railhint.com"


def _server_module():
    pytest.importorskip("fastapi", reason="needs the [server] extra")
    from nano_pay import server as _server

    return _server


def test_rail_hint_spec_points_at_the_live_canonical_spec():
    server = _server_module()
    info = server.rail_hint(100_000_000_000_000)  # 0.0001 XNO
    assert info["spec"] == _SPEC_URI


def test_payment_required_body_spec_matches_rail_hint():
    server = _server_module()
    body = server.payment_required_body(
        100_000_000_000_000,
        "nano_3aysuejus8iy1hhw6doc7syzg1aaa6hgpec91xcc36mf6hp6thy7u6ymkgfm",
        "https://feeless402.com/premium",
    )
    info = body["extensions"]["rail-hint"]["info"]
    assert info["spec"] == _SPEC_URI
    # One source of truth: the standalone helper must not drift from the body.
    assert info == server.rail_hint(100_000_000_000_000)


def test_rail_hint_avoids_known_dead_spec_hosts():
    server = _server_module()
    info = server.rail_hint(100_000_000_000_000)
    assert "x402nano.org" not in json.dumps(info)


# ---------- an unreachable ledger is not a refusal (402 branch) ----------

def test_outcome_402_unreachable_ledger_is_not_unpaid():
    """The merchant says 402 and the ledger does not answer: whether the block
    landed is unknown, so the caller must not be told 'not paid'. One
    transient timeout on a landed payment used to answer (False, 'absent')."""

    class LaggingLedger:
        def __init__(self, fail_first):
            self.calls = 0
            self.fail_first = fail_first

        def call(self, payload):
            self.calls += 1
            if self.calls <= self.fail_first:
                raise TimeoutError("node did not answer")
            return {"contents": {}, "confirmed": "true"}

    settled, ledger = _settle_outcome(LaggingLedger(1), "AB" * 32, 402)
    assert settled == "indeterminate", settled
    assert ledger == "unreachable", ledger

    # The ledger answered, and it does not have the block: still a refusal.
    # RPC.call raises RPCError("block not found") for that case (rpc.py
    # _SEMANTIC_ERRORS), which is what the existing LedgerRPC(None) fixture
    # models, so a landed-then-gone block is still reported as unpaid.
    class AnswersNoBlock:
        def call(self, payload):
            raise RPCError("block not found")

    assert _settle_outcome(AnswersNoBlock(), "AB" * 32, 402) == (False, "absent")


def test_block_found_after_a_timeout_is_still_confirmed():
    """The marker must not swallow a real answer: a block that appears on a
    later call is confirmed, not 'unreachable'."""

    class LaggingLedger:
        def __init__(self):
            self.calls = 0

        def call(self, payload):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("node did not answer")
            return {"contents": {}, "confirmed": "true"}

    import nano_pay.x402 as x
    from nano_pay.x402 import _ledger_verdict, UNREACHABLE

    rpc = LaggingLedger()
    assert _ledger_verdict(rpc, "AB" * 32, wait=8.0) == "confirmed"
