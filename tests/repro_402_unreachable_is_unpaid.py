"""Reproducer: a single ledger timeout at the 402 branch reports a landed payment as unpaid.

Run from the repository root:  python tests/repro_402_unreachable_is_unpaid.py

The claim under test (nano_pay/x402.py, _ledger_verdict + _settle_outcome at 90ddf4e):

  - _ledger_verdict documents three outcomes: "confirmed", "present",
    None ("not found / unreachable"). It reaches None both when the ledger
    answers "no such block" AND when rpc.call raises (line 67-68: any
    Exception -> verdict = None).
  - _settle_outcome asks it with wait=0.0 on the explicit-402 branch
    (line 92-94), so exactly one rpc.call is made.
  - If that single call raises, _ledger_verdict returns None, and
    _settle_outcome turns None into `False, "absent"` (line 97-98).

So one transient ledger timeout, on a payment that HAS landed, is reported to
the caller as "not paid". The client's own docstring calls that state the one
that "invites a caller to pay a second time" (the defect fixed in PR #9 for a
different cause, the 3 s / 8 s split).

This reproducer makes rpc.call raise for the first two calls and answer
"confirmed" afterwards: a lagging node, not an absent block. On the pre-fix
code the 402 branch answers (False, "absent"); with a correct treatment of
"unreachable" it must not answer False for a block that is on the ledger.

No network, no wallet, no funds.
"""
import sys, os, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nano_pay.x402 import _settle_outcome, _ledger_verdict
from nano_pay import verify as v

HASH = "AB" * 32


class LaggingLedger:
    """The node is briefly unreachable, then has the block (confirmed).

    The block IS on the ledger throughout; only the node's availability
    changes. That is what a timeout looks like from the client.
    """

    def __init__(self, fail_first=2):
        self.calls = 0
        self.fail_first = fail_first

    def call(self, payload):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise TimeoutError("node did not answer within the client's timeout")
        return {"contents": {}, "confirmed": "true"}


print("1) what _ledger_verdict says for an unreachable node, no wait:")
rpc = LaggingLedger(fail_first=99)
print("   _ledger_verdict(wait=0.0, node raises) ->", _ledger_verdict(rpc, HASH, wait=0.0))
print("   (docstring says None = 'not found / unreachable' — indistinguishable)")

print()
print("2) the 402 branch on a payment that is on the ledger but the node lagged once:")
time.sleep = time.sleep  # keep the name honest, no patch games
rpc = LaggingLedger(fail_first=1)
settled, ledger = _settle_outcome(rpc, HASH, 402)
print(f"   _settle_outcome(LaggingLedger, 402) -> ({settled!r}, {ledger!r})")
print(f"   rpc calls made: {rpc.calls}  (wait=0.0 -> exactly one look)")

print()
print("3) the same block, asked once more when the node answers:")
rpc2 = LaggingLedger(fail_first=0)
print("   _settle_outcome(answering node, 402) ->", _settle_outcome(rpc2, HASH, 402))

violation = (settled is False)
print()
print("CONTRACT (a landed block must not be reported unpaid because one node call timed out):",
      "VIOLATED" if violation else "honoured")
if violation:
    print("DEFECT: the payment is on the ledger; a single transient node timeout on the 402")
    print("        branch answers False/'absent', the state the caller reads as 'pay again'.")
    sys.exit(1)
print("no defect on this path")
sys.exit(0)
