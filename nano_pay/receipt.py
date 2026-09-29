"""Standalone settlement-receipt verifier for Nano (XNO) x402 payments.

This is the piece an outside pay-per-call seller asks for first: "how do I
check that the payment actually arrived, without running a Nano node or
trusting your word?"

It takes only a block hash, the amount we quoted, and the account we expected
to be paid, plus any public Nano RPC endpoint, and answers `settled` — true
only when the block is confirmed on the public ledger, the amount matches
the quote, and it paid the account the seller expected.

    Receipt = verify(block_hash, expect_raw, account, rpc_url)

    if receipt.settled:
        # charge the caller, or ship the resource
    else:
        # nothing moved; do not ship

Deliberately dependency-free beyond the rest of the package: it talks to the
public ledger over the same RPC client the merchant server already uses, and
never moves funds — it only reads.
"""

from dataclasses import dataclass, asdict

from nano_pay.rpc import RPC, RPCError


class NotFound(Exception):
    """The node reports no block with this hash on the public ledger."""

    def __init__(self, block_hash: str):
        self.block_hash = block_hash
        super().__init__(f"no block with hash {block_hash} on the ledger")


class LedgerUnreachable(Exception):
    """No node could be asked: the ledger's answer could not be determined.

    Distinct from `NotFound` on purpose. `NotFound` is the ledger saying
    "there is no such block"; this is the verifier saying "I could not ask".
    A seller that ships on "settled" and refuses on "not settled" must not
    treat an unreachable node as a refusal: the block may be confirmed while
    every endpoint is down. An outcome that could not be determined is never
    reported as "did not happen" (the same rule x402.py and verify.py state,
    x402 #3208). Retry, or read the hash from another node, before giving up.
    """

    def __init__(self, detail: str = ""):
        self.detail = detail
        super().__init__(
            "could not reach the ledger to check this block"
            + (f": {detail}" if detail else "")
        )


class Mismatch(Exception):
    """The block settled, but the amount or the destination differs from what
    was expected. The seller must not honor it."""

    def __init__(self, got, expected, what: str):
        self.got = got
        self.expected = expected
        self.what = what
        super().__init__(f"{what} mismatch: got {got!r}, expected {expected!r}")


@dataclass(frozen=True)
class Receipt:
    """A verified settlement. `to_json` is the stable wire shape."""

    settled: bool
    amount_raw: int
    height: int
    account: str

    def to_json(self) -> str:
        import json

        return json.dumps(asdict(self), sort_keys=True)


def _lookup(block_hash: str, rpc_urls, timeout: float = 20.0) -> dict:
    """Ask the ledger for a block, returning the raw block_info reply or None
    if the node says it does not know this hash.

    A semantic "block not found" is the ledger answering; every other failure
    (all endpoints down, timeout, HTTP error, bad JSON) means the ledger could
    not be asked and raises `LedgerUnreachable`, so the caller never reads an
    unreachable node as "this block is not on the ledger".
    """
    rpc = RPC(urls=rpc_urls, timeout=timeout)
    try:
        return rpc.call(
            {"action": "block_info", "json_block": "true", "hash": block_hash}
        )
    except RPCError as e:
        if "block not found" in str(e).lower():
            return None
        raise LedgerUnreachable(str(e)) from e


def verify(block_hash: str, expect_raw: int, account: str, rpc_url: str) -> Receipt:
    """Verify that a Nano send block settled, paid the expected account in the
    expected amount, and return a `Receipt`.

    Raises:
        NotFound: the node reports no block with this hash.
        LedgerUnreachable: no node could be asked. Distinct from NotFound: the
            block may be settled while every endpoint is down.
        Mismatch: the block is confirmed but the amount or destination differs
            from what was expected.
    """
    info = _lookup(block_hash, [rpc_url])
    if info is None or "block_account" not in info:
        raise NotFound(block_hash)

    block_account = str(info.get("block_account", "")).lower()
    if block_account != account.lower():
        raise Mismatch(block_account, account, "destination account")

    amount_raw = int(info.get("amount", 0))
    if amount_raw != int(expect_raw):
        raise Mismatch(amount_raw, expect_raw, "amount")

    confirmed = str(info.get("confirmed", "")).lower() == "true"
    height = int(info.get("height", 0))

    return Receipt(
        settled=confirmed,
        amount_raw=amount_raw,
        height=height,
        account=info.get("block_account", account),
    )
