"""Nano node RPC client with public-node failover."""

import requests

DEFAULT_RPCS = [
    "https://rpc.nano.to",
    "https://node.somenano.com/proxy",
    "https://rainstorm.city/api",
    "https://nanoslo.0x.no/proxy",
]

# RPC errors that describe the request/account, not a broken node —
# failing over to another node would return the same thing.
_SEMANTIC_ERRORS = ("account not found", "block not found")


class RPCError(Exception):
    pass


class RPC:
    def __init__(self, urls=None, timeout=20):
        self.urls = urls or list(DEFAULT_RPCS)
        self.timeout = timeout

    def call(self, payload: dict) -> dict:
        last_err = None
        for url in self.urls:
            try:
                r = requests.post(url, json=payload, timeout=self.timeout)
                data = r.json()
                if isinstance(data, dict) and "error" in data:
                    err = str(data["error"])
                    if err.lower().strip() in _SEMANTIC_ERRORS:
                        raise RPCError(err)
                    last_err = f"{url}: {err}"
                    continue
                if r.status_code >= 400:
                    last_err = f"{url}: HTTP {r.status_code}"
                    continue
                return data
            except RPCError:
                raise
            except Exception as e:  # network / JSON errors -> try next node
                last_err = f"{url}: {e}"
        raise RPCError(f"all RPC nodes failed, last error: {last_err}")

    def account_info(self, addr: str):
        """Return account_info dict, or None if the account is unopened."""
        try:
            return self.call(
                {"action": "account_info", "account": addr, "representative": "true"}
            )
        except RPCError as e:
            if "account not found" in str(e).lower():
                return None
            raise

    def receivable(self, addr: str, count=50) -> dict:
        """Return {block_hash: raw_amount} of pending incoming sends."""
        for action in ("receivable", "pending"):
            try:
                res = self.call(
                    {
                        "action": action,
                        "account": addr,
                        "count": str(count),
                        "threshold": "1",
                    }
                )
                blocks = res.get("blocks") or {}
                if isinstance(blocks, list):  # some nodes: list without threshold
                    return {h: 0 for h in blocks}
                return {h: int(a) for h, a in blocks.items()}
            except RPCError as e:
                if "unknown command" in str(e).lower():
                    continue
                raise
        return {}

    def process(self, block_dict: dict, subtype: str) -> str:
        """Broadcast a block. Returns the block hash."""
        res = self.call(
            {
                "action": "process",
                "json_block": "true",
                "subtype": subtype,
                "block": block_dict,
            }
        )
        return res["hash"]

    def work_generate(self, root: str, difficulty: str):
        """Ask nodes for work; many public nodes refuse — return None then."""
        for url in self.urls:
            try:
                r = requests.post(
                    url,
                    json={
                        "action": "work_generate",
                        "hash": root,
                        "difficulty": difficulty,
                    },
                    timeout=self.timeout,
                )
                data = r.json()
                if isinstance(data, dict) and data.get("work"):
                    return data["work"]
            except Exception:
                pass
        return None
