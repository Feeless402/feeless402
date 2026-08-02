"""Self-custodied single-account Nano wallet backed by a local JSON file.

Design notes:
- Seed lives in ~/.nano-pay/wallet.json (chmod 600). One account (index 0).
- Balance/frontier are always re-synced from the network before use; the
  file never stores authoritative chain state.
- Work (PoW) for the current frontier is cached so the next block is
  instant; after each broadcast we pre-compute work for the new frontier.
- x402 payments sign a send block but do NOT broadcast it — the resource
  server settles it via its facilitator. Until we see a success response,
  the frontier must not be treated as advanced (see X402 client).
"""

import json
import os
from pathlib import Path

import nanopy

from . import raw_to_xno

DEFAULT_DIR = Path(os.environ.get("NANO_PAY_HOME", Path.home() / ".nano-pay"))
# Well-established representative (Nano Foundation-adjacent); user-overridable.
DEFAULT_REP = "nano_1center16ci77qw5w69ww8sy4i4bfmgfhr81ydzpurm91cauj11jn6y3uc5y"
NET = nanopy.Network()


class WalletError(Exception):
    pass


class Wallet:
    def __init__(self, path: Path = None):
        self.path = Path(path) if path else DEFAULT_DIR / "wallet.json"
        self.data = None

    # ---------- persistence ----------

    def exists(self) -> bool:
        return self.path.exists()

    def create(self, seed: str = None):
        if self.exists():
            raise WalletError(f"wallet already exists at {self.path}")
        seed = seed or os.urandom(32).hex()
        if len(bytes.fromhex(seed)) != 32:
            raise WalletError("seed must be 64 hex chars")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = {
            "seed": seed,
            "index": 0,
            "representative": DEFAULT_REP,
            "work_cache": {},  # {frontier_or_pk: work}
        }
        self._save()
        return self

    def load(self):
        if not self.exists():
            raise WalletError(
                f"no wallet at {self.path} — run `nano-pay init` first"
            )
        self.data = json.loads(self.path.read_text())
        return self

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    # ---------- account / state ----------

    @property
    def account(self) -> nanopy.Account:
        sk = nanopy.deterministic_key(self.data["seed"], self.data["index"])
        return nanopy.Account(sk=sk)

    @property
    def address(self) -> str:
        return self.account.addr

    def synced_account(self, rpc) -> nanopy.Account:
        """Account object with frontier/balance/rep loaded from the network."""
        acct = self.account
        info = rpc.account_info(acct.addr)
        if info is None:  # unopened account
            acct.frontier = "0" * 64
            acct.raw_bal = 0
            acct.rep = nanopy.Account(addr=self.data["representative"])
        else:
            acct.frontier = info["frontier"]
            acct.raw_bal = int(info["balance"])
            acct.rep = nanopy.Account(addr=info["representative"])
        return acct

    # ---------- work ----------

    def _work_root(self, acct: nanopy.Account) -> str:
        return acct.pk if acct.frontier == "0" * 64 else acct.frontier

    @staticmethod
    def _work_valid(work: str, root: str, difficulty: str) -> bool:
        return bool(
            nanopy.ext.work_validate(
                int(work, 16), bytes.fromhex(root), int(difficulty, 16)
            )
        )

    def get_work(self, root: str, difficulty: str, rpc=None) -> str:
        cached = self.data["work_cache"].get(root)
        if cached and self._work_valid(cached, root, difficulty):
            return cached
        if rpc:
            w = rpc.work_generate(root, difficulty)
            if w and self._work_valid(w, root, difficulty):
                self.data["work_cache"][root] = w
                self._save()
                return w
        w = f"{nanopy.ext.work_generate(bytes.fromhex(root), int(difficulty, 16), os.urandom(128)):016x}"
        self.data["work_cache"][root] = w
        self._save()
        return w

    def prework(self, new_frontier: str, rpc=None):
        """Pre-compute send-difficulty work for a frontier so the next
        block is instant. Send difficulty also satisfies receive blocks."""
        self.get_work(new_frontier, NET.send_difficulty, rpc)

    def drop_work(self, root: str):
        if self.data["work_cache"].pop(root, None) is not None:
            self._save()

    # ---------- operations ----------

    def receive_all(self, rpc, prework=True) -> list:
        """Pocket all pending incoming sends. Returns list of (hash, raw)."""
        received = []
        pending = rpc.receivable(self.address)
        for send_hash, raw_amt in pending.items():
            acct = self.synced_account(rpc)
            root = self._work_root(acct)
            work = self.get_work(root, NET.receive_difficulty, rpc)
            blk = acct.receive(send_hash, raw_amt, work=work)
            rpc.process(blk.dict_, "receive")
            self.drop_work(root)
            received.append((blk.hash_, raw_amt))
        if received and prework:
            acct = self.synced_account(rpc)
            self.prework(self._work_root(acct), rpc)
        return received

    def send(self, rpc, to_addr: str, raw_amt: int, prework=True) -> str:
        """Direct send (broadcast by us). Returns block hash."""
        acct = self.synced_account(rpc)
        if acct.raw_bal < raw_amt:
            raise WalletError(
                f"insufficient balance: have {raw_to_xno(acct.raw_bal)} XNO, "
                f"need {raw_to_xno(raw_amt)} XNO"
            )
        root = self._work_root(acct)
        work = self.get_work(root, NET.send_difficulty, rpc)
        blk = acct.send(nanopy.Account(addr=to_addr), raw_amt, work=work)
        h = rpc.process(blk.dict_, "send")
        self.drop_work(root)
        if prework:
            self.prework(blk.hash_, rpc)
        return h

    def build_payment_block(self, rpc, to_addr: str, raw_amt: int):
        """Sign a send block WITHOUT broadcasting (x402: server settles it).

        Returns (block_dict, new_frontier, work_root). Caller must call
        payment_succeeded()/payment_failed() afterwards.
        """
        acct = self.synced_account(rpc)
        if acct.raw_bal < raw_amt:
            raise WalletError(
                f"insufficient balance: have {raw_to_xno(acct.raw_bal)} XNO, "
                f"need {raw_to_xno(raw_amt)} XNO"
            )
        root = self._work_root(acct)
        work = self.get_work(root, NET.send_difficulty, rpc)
        blk = acct.send(nanopy.Account(addr=to_addr), raw_amt, work=work)
        return blk.dict_, blk.hash_, root

    def payment_succeeded(self, rpc, new_frontier: str, work_root: str):
        self.drop_work(work_root)
        self.prework(new_frontier, rpc)

    def payment_failed(self, work_root: str):
        # Keep cached work (frontier unchanged), but note the fork hazard:
        # a signed-but-unsettled block for this frontier is in the wild.
        # The next build re-syncs from the network, so if the server settles
        # late, we pick up the new frontier; if we sign a competing block
        # first, only one of the two can ever confirm (funds stay ours).
        pass
