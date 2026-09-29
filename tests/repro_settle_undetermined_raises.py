"""Reproducer: settle_block calls a broadcast a failure when it could not be determined.

Run from the repository root:  python tests/repro_settle_undetermined_raises.py

settle_block's own docstring states the obligation (x402 #3208):

    "A rejected or lost broadcast is NOT proof the block did not land: ... a
     timeout after acceptance looks identical to a failure. Since the hash is
     known before broadcast, ask the ledger before calling it a failure — an
     outcome that could not be determined must never be reported as
     'did not happen'."

The code does exactly that — except that the ledger question it asks cannot
tell "the ledger says no such block" from "the ledger did not answer", because
_in_ledger (nano_pay/verify.py) collapses any exception into False:

    def _in_ledger(rpc, h: str) -> bool:
        try:
            info = rpc.call({"action": "block_info", "json_block": "true", "hash": h})
        except Exception:
            return False
        return isinstance(info, dict) and "contents" in info

and the call site reads that False as "the block is not there":

    try:
        rpc.process(block, "send")
    except Exception:
        if not _in_ledger(rpc, h):
            raise                       # <-- "did not happen", on an undetermined outcome

So a process call that times out while the ledger is also unreachable raises
out of settle_block, and the merchant reports the payment as failed — the
exact report the docstring forbids. The block may be on the chain; the caller
will be told to pay again.

This is the same distinction PR #10 fixes for the 402 branch of
_settle_outcome (an unreachable ledger is not a refusal). Here it is the
broadcast path: an unreachable ledger is not "the block did not land".

No network, no wallet, no funds: rpc is a fake that fails both calls.

Run:  python3 tests/repro_settle_undetermined_raises.py
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nano_pay import verify as V
NET = V.NET

PAYER = "nano_1cq4uu7mspxtwmhrjtk4gwk44p1mjpk6dsjc4ocki5wp7ym5ts14dw9gtpds"
PAY_TO = "nano_1xug1q5t7nxoj3ywwzokiea9jz8fq8qfgzp8pbyfr3co3e5xgj755uofu8ue"
LINK_PK = NET.to_pk(PAY_TO).upper()
PREV = "4DA37CC62F040730D14E9D57A83D3810C54CBFF1C7A389E477F5A290B28A688F"


class NodeDownBothWays:
    """process times out AND block_info is unreachable: the outcome is unknown.

    This is not "the block is absent". The node never got to answer.
    """

    def __init__(self):
        self.process_calls = 0
        self.info_calls = 0

    def process(self, block, subtype):
        self.process_calls += 1
        raise TimeoutError("node did not answer the process call")

    def call(self, payload):
        self.info_calls += 1
        raise TimeoutError("node did not answer the block_info call")


def the_block():
    return {
        "type": "state", "account": PAYER, "previous": PREV, "representative": PAYER,
        "balance": str(10 ** 30), "link": LINK_PK,
        "signature": "00" * 64, "work": "ff00000000000000",
    }


rpc = NodeDownBothWays()
outcome = None
raised = None
try:
    outcome = V.settle_block(the_block(), rpc, confirm_timeout=0.0)
except Exception as e:
    raised = f"{type(e).__name__}: {e}"

print("node: process timed out, then block_info was unreachable (outcome undetermined)")
print(f"  process calls: {rpc.process_calls}, block_info calls: {rpc.info_calls}")
print()
print("settle_block returned:", outcome)
print("settle_block raised:  ", raised)
print()

if raised:
    print("VIOLATED: the outcome could not be determined (the node never answered),")
    print("          and settle_block raised — which the caller reads as 'did not happen'.")
    print("          The block may be on the chain; the payer is invited to pay again.")
    sys.exit(1)

print("honoured: an undetermined outcome is reported as undetermined")
sys.exit(0)
