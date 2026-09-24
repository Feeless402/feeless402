"""Offline tests for the standalone settlement-receipt verifier.

No network, no real funds: `_lookup` (the only thing that touches an RPC
node) is patched with canned block_info replies so every behaviour is
exercised deterministically.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nano_pay import receipt
from nano_pay.receipt import Mismatch, NotFound, Receipt, verify

BLOCK_HASH = "A" * 64
ACCOUNT = "nano_1puq5g8eqy1h8z1w9zqy6q7x9tq9y6u9t9y5p6q7x9tq9y6u9t9y5p6q7x9tq"
OTHER_ACCOUNT = "nano_3i1p3k1x9b7y1w8z2qy6q7x9tq9y6u9t9y5p6q7x9tq9y6u9t9y5p6q7x9tq"


def settled_info(amount=1000, height=42, account=ACCOUNT, confirmed=True):
    return {
        "block_account": account,
        "amount": str(amount),
        "confirmed": "true" if confirmed else "false",
        "height": str(height),
    }


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(receipt, "_lookup", lambda *a, **k: _fake_lookup())


_FAKE = {"reply": None}


def _fake_lookup():
    return _FAKE["reply"]


def test_settled_receipt():
    _FAKE["reply"] = settled_info()
    r = verify(BLOCK_HASH, 1000, ACCOUNT, "https://example.invalid/rpc")
    assert r.settled is True
    assert r.amount_raw == 1000
    assert r.height == 42
    assert r.account == ACCOUNT


def test_missing_block():
    _FAKE["reply"] = None
    with pytest.raises(NotFound):
        verify(BLOCK_HASH, 1000, ACCOUNT, "https://example.invalid/rpc")


def test_mismatch_amount():
    _FAKE["reply"] = settled_info(amount=999)
    with pytest.raises(Mismatch) as ei:
        verify(BLOCK_HASH, 1000, ACCOUNT, "https://example.invalid/rpc")
    assert ei.value.what == "amount"


def test_mismatch_account():
    _FAKE["reply"] = settled_info(account=OTHER_ACCOUNT)
    with pytest.raises(Mismatch) as ei:
        verify(BLOCK_HASH, 1000, ACCOUNT, "https://example.invalid/rpc")
    assert ei.value.what == "destination account"


def test_receipt_to_json():
    r = Receipt(settled=True, amount_raw=1000, height=42, account=ACCOUNT)
    j = r.to_json()
    assert '"amount_raw": 1000' in j
    assert '"height": 42' in j
    assert '"settled": true' in j
    assert 'account' in j and ACCOUNT in j
