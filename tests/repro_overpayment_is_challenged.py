"""Reproducer: a payer who sends more than the price is challenged again (402) and can pay twice.

Run from the repository root:  python tests/repro_overpayment_is_challenged.py

The server's own 402 challenge says:

    "Send at least price_raw to pay_to, then retry with header X-Nano-Payment: ..."
    "Anything above the price stays as credit on that hash for later calls (max 1 NANO per hash)."

"at least" and "anything above the price stays as credit" both promise that a
send larger than the price is a valid payment.

The payment path does not honour that:

  - verify_block (nano_pay/verify.py, line 57-61) requires STRICT equality:
        decrease = int(info["balance"]) - int(block["balance"])
        if decrease != amount_raw:
            raise PaymentInvalid(f"balance decrease {decrease} != required {amount_raw}")
    A block that sends price + delta is refused with PaymentInvalid, which the
    server turns into a fresh 402.
  - settled_replay (nano_pay/verify.py, line 175-176) has the same strict test:
        if info.get("subtype") != "send" or int(info.get("amount") or 0) != amount_raw:
            return None
    So the same block, re-presented after it settled, does NOT get the settled
    state and the resource (x402 #3325 §5.3.5: "never with a fresh challenge")
    — it falls through to verify_block and its 402 again.

The caller that followed the challenge literally ("send at least the price")
is therefore told to pay again, for a payment that is on the ledger. That is
the double-payment shape this module elsewhere works hard to avoid.

This reproducer uses only the pure functions and a fake ledger: no network, no
wallet, no funds. It asserts both halves:
  A) settled_replay must honour an overpayment that pays this server (it does not);
  B) verify_block must accept an overpayment (it does not).

Run:  python3 tests/repro_overpayment_is_challenged.py
"""
import os, sys, types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# nanopy / network may need a node; the amounts path does not. Keep imports light.
from nano_pay import verify as V
NET = V.NET  # nanopy.Network(), same object the module uses

PRICE = 10 ** 27              # the price the server asks, 0.001 XNO
OVERPAY = PRICE + 5 * 10 ** 24  # 0.005 XNO more: "anything above the price stays as credit"

PAYER = "nano_1cq4uu7mspxtwmhrjtk4gwk44p1mjpk6dsjc4ocki5wp7ym5ts14dw9gtpds"
PAY_TO = "nano_1xug1q5t7nxoj3ywwzokiea9jz8fq8qfgzp8pbyfr3co3e5xgj755uofu8ue"
HASH = "7BE6C046A29BCB84111B07682A4A31489ECDF21AD433FF988E08C6750DBCE853"
LINK_PK = NET.to_pk(PAY_TO).upper()
PREV = "4DA37CC62F040730D14E9D57A83D3810C54CBFF1C7A389E477F5A290B28A688F"


class LedgerWithTheOverpaidBlock:
    """The block is on the ledger, confirmed, and pays this server OVERPAY."""

    def __init__(self):
        self.calls = []

    def call(self, payload):
        self.calls.append(payload)
        if payload.get("action") == "block_info":
            return {
                "confirmed": "true",
                "subtype": "send",
                "amount": str(OVERPAY),
                "local_timestamp": str(int(__import__("time").time())),
                "contents": {"account": PAYER, "link": LINK_PK, "balance": "0"},
            }
        raise AssertionError("unexpected rpc " + str(payload.get("action")))

    def account_info(self, addr):
        # the payer's frontier is the block's previous (the block is next in line),
        # and the balance already reflects the send: decrease == OVERPAY
        return {"frontier": PREV, "balance": str(10 ** 30)}


def block_for_overpay():
    return {
        "type": "state", "account": PAYER, "previous": PREV, "representative": PAYER,
        "balance": str(10 ** 30 - OVERPAY), "link": LINK_PK,
        "signature": "00" * 64, "work": "ff00000000000000",
    }


print("challenge text promises 'at least price_raw' and credit above it.")
print(f"  price   = {PRICE}")
print(f"  overpay = {OVERPAY}  (price + {OVERPAY - PRICE})")
print()

# --- A) settled_replay on an overpaid, settled block -------------------------
led = LedgerWithTheOverpaidBlock()
res = V.settled_replay(block_for_overpay(), PRICE, PAY_TO, led, requester="1.2.3.4")
print("A) settled_replay(overpaid settled block) ->", res)
a_ok = isinstance(res, dict) and res.get("success") is True

# --- B) verify_block on the same overpayment ---------------------------------
led2 = LedgerWithTheOverpaidBlock()
try:
    payer = V.verify_block(block_for_overpay(), PRICE, PAY_TO, led2)
    b_ok, b_err = True, payer
except V.PaymentInvalid as e:
    b_ok, b_err = False, str(e)
except Exception as e:
    b_ok, b_err = False, f"{type(e).__name__}: {e}"
print(f"B) verify_block(overpaid block) -> {'accepted' if b_ok else 'REFUSED: ' + str(b_err)}")
print()

print("A: a settled overpayment must be answered with its settled state (§5.3.5)", "->", "OK" if a_ok else "VIOLATED (returns None -> fresh 402)")
print("B: an overpayment must be accepted ('at least the price')", "->", "OK" if b_ok else "VIOLATED (PaymentInvalid -> 402)")
print()
if a_ok and b_ok:
    print("no defect on this path")
    sys.exit(0)
print("DEFECT: the 402 challenge says 'at least price_raw', the payment path requires exactly")
print("        price_raw. A payer who sends more is challenged again and can pay twice for one call.")
sys.exit(1)
