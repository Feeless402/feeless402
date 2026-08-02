"""Merchant-side verification + settlement of x402 Nano payments.

Implements the checks from the x402nano `exact` scheme spec:
  1. payer's confirmed frontier == block.previous (no forks, no replays)
  2. balance decrease == required amount
  3. block.link decodes to OUR payTo account
  4. ed25519-blake2b signature verifies against payer's public key
  5. work present (format check; the network enforces difficulty)
Then settles by broadcasting the block ourselves (no facilitator needed)
and polling for confirmation.
"""

import time

import nanopy

NET = nanopy.Network()


class PaymentInvalid(Exception):
    pass


# Frontiers we've already accepted a block for (fork/replay guard).
_seen_previous: dict = {}


def _prune_seen(max_age=3600):
    cutoff = time.time() - max_age
    for k in [k for k, v in _seen_previous.items() if v < cutoff]:
        _seen_previous.pop(k, None)


def verify_block(block: dict, amount_raw: int, pay_to_addr: str, rpc) -> str:
    """Validate a payment block against our requirements. Returns payer addr."""
    if not isinstance(block, dict) or block.get("type") != "state":
        raise PaymentInvalid("payload.block must be a state block")

    payer = block.get("account", "")
    prev = str(block.get("previous", "")).upper()
    try:
        payer_acct = nanopy.Account(addr=payer)
    except Exception:
        raise PaymentInvalid("invalid payer account address")

    # 3. link must be our treasury
    our_pk = NET.to_pk(pay_to_addr).upper()
    if str(block.get("link", "")).upper() != our_pk:
        raise PaymentInvalid("block.link does not pay this server")

    # 1. frontier check + 2. amount check
    info = rpc.account_info(payer)
    if info is None:
        raise PaymentInvalid("payer account not found on ledger")
    if info["frontier"].upper() != prev:
        raise PaymentInvalid("block.previous is not the payer's frontier")
    decrease = int(info["balance"]) - int(block["balance"])
    if decrease != amount_raw:
        raise PaymentInvalid(
            f"balance decrease {decrease} != required {amount_raw}"
        )

    # replay/fork guard
    _prune_seen()
    if prev in _seen_previous:
        raise PaymentInvalid("a block on this frontier was already accepted")

    # 4. signature
    b = nanopy.StateBlock(
        payer_acct,
        nanopy.Account(addr=block["representative"]),
        int(block["balance"]),
        prev,
        str(block["link"]).upper(),
        sig=block.get("signature", ""),
        work=block.get("work", ""),
    )
    if not b.verify_signature():
        raise PaymentInvalid("block signature invalid")

    # 5. work present
    try:
        assert len(bytes.fromhex(block.get("work", ""))) == 8
    except Exception:
        raise PaymentInvalid("work missing or malformed")

    return payer


def settle_block(block: dict, rpc, confirm_timeout=8.0) -> dict:
    """Broadcast the verified block and poll for confirmation."""
    h = rpc.process(block, "send")
    _seen_previous[str(block["previous"]).upper()] = time.time()
    confirmed = False
    deadline = time.time() + confirm_timeout
    while time.time() < deadline:
        try:
            info = rpc.call(
                {"action": "block_info", "json_block": "true", "hash": h}
            )
            if str(info.get("confirmed")).lower() == "true":
                confirmed = True
                break
        except Exception:
            pass
        time.sleep(0.5)
    return {"success": True, "hash": h, "confirmed": confirmed,
            "network": "nano:mainnet"}
