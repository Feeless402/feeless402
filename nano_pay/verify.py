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


def block_hash(block: dict) -> str:
    """Hash of a state block. Computable before broadcast, so the ledger —
    not the RPC reply — can be the source of truth for whether it landed."""
    b = nanopy.StateBlock(
        nanopy.Account(addr=block["account"]),
        nanopy.Account(addr=block["representative"]),
        int(block["balance"]),
        str(block["previous"]).upper(),
        str(block["link"]).upper(),
        sig=block.get("signature", ""),
        work=block.get("work", ""),
    )
    return b.hash_.upper()


def _in_ledger(rpc, h: str) -> bool:
    try:
        info = rpc.call({"action": "block_info", "json_block": "true", "hash": h})
    except Exception:
        return False
    return isinstance(info, dict) and "contents" in info


# Re-presented authorizations already honored: hash -> times served.
_replays: dict = {}
REPLAY_WINDOW_S = 15 * 60   # a block confirmed longer ago than this is history, not a retry
REPLAY_MAX = 3              # a crashed client needs one re-delivery; three is generous


def settled_replay(block: dict, amount_raw: int, pay_to_addr: str, rpc):
    """Receiver obligation (x402 #3325 §5.3.5): an authorization that has
    already settled is answered with its settled state — and the resource —
    never with a fresh challenge.

    A client that paid, then died before reading the reply, re-presents the
    SAME signed block on restart. Refusing it ("previous is not the payer's
    frontier") is exactly the fresh challenge that turns one purchase into
    two. So: if the presented block's hash is confirmed on the ledger, pays
    this server, and carries the price, return a receipt for it.

    Block contents are public once confirmed, so a third party could replay
    one too. Two bounds keep that to a curiosity: only blocks confirmed
    within REPLAY_WINDOW_S qualify, and each hash is honored REPLAY_MAX
    times. Returns a receipt dict (with 'payer') or None to fall through to
    normal verification."""
    try:
        h = block_hash(block)
        info = rpc.call({"action": "block_info", "json_block": "true", "hash": h})
    except Exception:
        return None
    if not isinstance(info, dict) or str(info.get("confirmed")).lower() != "true":
        return None
    c = info.get("contents") or {}
    if str(c.get("link", "")).upper() != NET.to_pk(pay_to_addr).upper():
        return None
    if info.get("subtype") != "send" or int(info.get("amount") or 0) != amount_raw:
        return None
    ts = int(info.get("local_timestamp") or 0)
    if ts and time.time() - ts > REPLAY_WINDOW_S:
        return None
    if _replays.get(h, 0) >= REPLAY_MAX:
        return None
    _replays[h] = _replays.get(h, 0) + 1
    return {"success": True, "hash": h, "confirmed": True,
            "network": "nano:mainnet", "replay": True, "payer": c.get("account", "")}


def settle_block(block: dict, rpc, confirm_timeout=8.0) -> dict:
    """Broadcast the verified block and poll for confirmation.

    A rejected or lost broadcast is NOT proof the block did not land: a node
    that already holds it answers "Old block" (or its subtype pre-check), and
    a timeout after acceptance looks identical to a failure. Since the hash
    is known before broadcast, ask the ledger before calling it a failure —
    an outcome that could not be determined must never be reported as
    "did not happen" (x402 #3208 receiver obligation).
    """
    h = block_hash(block)
    try:
        rpc.process(block, "send")
    except Exception:
        if not _in_ledger(rpc, h):
            raise
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
