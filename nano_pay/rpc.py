"""Nano node RPC client with public-node failover."""

import os

import requests

# Optional dedicated proof-of-work service, tried before the public nodes.
# Public nodes serve work in ~5s and many refuse outright, which caps how
# fast a faucet or merchant can sign blocks. Set:
#   F402_WORK_URLS  comma-separated work endpoints
#   F402_WORK_KEY   sent as {"key": ...} and as a Bearer header (services differ)
WORK_URLS = [u.strip() for u in os.environ.get("F402_WORK_URLS", "").split(",")
             if u.strip()]
WORK_KEY = os.environ.get("F402_WORK_KEY", "")

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
    def __init__(self, urls=None, timeout=20, work_urls=None, work_key=None):
        self.urls = urls or list(DEFAULT_RPCS)
        self.timeout = timeout
        self.work_urls = work_urls if work_urls is not None else list(WORK_URLS)
        self.work_key = work_key if work_key is not None else WORK_KEY

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
        """Ask a work service first, then nodes; many public nodes refuse.

        Returns None if nobody will do it, and the caller falls back to
        local PoW.
        """
        payload = {"action": "work_generate", "hash": root,
                   "difficulty": difficulty}
        for url in list(self.work_urls) + list(self.urls):
            body = dict(payload)
            headers = {}
            if url in self.work_urls and self.work_key:
                body["key"] = self.work_key            # BoomPoW-style
                headers["Authorization"] = f"Bearer {self.work_key}"
            try:
                r = requests.post(url, json=body, headers=headers,
                                  timeout=self.timeout)
                data = r.json()
                if isinstance(data, dict) and data.get("work"):
                    return data["work"]
            except Exception:
                pass
        return None
