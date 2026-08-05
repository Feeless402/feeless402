#!/usr/bin/env python3
"""Telegram alert when a REAL (external) agent claims from the Feeless402 faucet.

Read-only on the faucet ledger; keeps its own tiny state file. Fires only when a
new NON-localhost IP appears (a stranger's agent) — local/test claims (127.0.0.1)
are ignored so it never cries wolf. Sends via the same Telegram bot Vane uses.
Run every few minutes from cron. Best-effort; never raises.
"""
import json
import os
from pathlib import Path

import requests

LEDGER = Path.home() / ".nano-pay" / "faucet-ledger.json"
STATE = Path.home() / ".nano-pay" / "faucet-alert-state.json"
FAUCET_XNO = float(os.getenv("F402_FAUCET_XNO", "0.005"))
LOCAL = {"127.0.0.1", "::1", "localhost", "unknown", ""}
# Operator self-traffic IPs (server, home) live outside the repo:
# ~/.nano-pay/self-ips.txt, one IP per line.
try:
    LOCAL |= {l.strip() for l in
              (Path.home() / ".nano-pay" / "self-ips.txt").read_text().splitlines()
              if l.strip() and not l.startswith("#")}
except Exception:
    pass

def _env_fallback(key: str) -> str:
    """Env var first, else KEY=VALUE lines in ~/.nano-pay/telegram.env."""
    if os.getenv(key):
        return os.environ[key]
    try:
        for line in (Path.home() / ".nano-pay" / "telegram.env").read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip()
    except Exception:
        pass
    return ""


_TOKEN = _env_fallback("TELEGRAM_TOKEN")
_CHAT_ID = _env_fallback("TELEGRAM_CHAT_ID")


def tg(text: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{_TOKEN}/sendMessage",
            json={"chat_id": _CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=6,
        )
    except Exception:
        pass


def load(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _external_addrs(led) -> set:
    """Addresses whose claim time matches (±2s) a non-local IP's claim time.

    The ledger stores addresses and IPs in separate maps with no direct link;
    claim timestamps are written from the same time.time() call, so proximity
    is the association.
    """
    ext_times = [t for ip, ts in led.get("ips", {}).items()
                 if ip not in LOCAL for t in ts]
    return {a for a, at in led.get("addresses", {}).items()
            if any(abs(at - t) < 2 for t in ext_times)}


# Warn while there's still time to refill, not when an agent hits a 503.
# Crossed downward once each; re-arms if the faucet is topped back up.
LOW_MARKS = [500, 100, 0]


def check_refill_needed(st) -> None:
    """Ping if the faucet's remaining claims crossed a low-water mark."""
    try:
        r = requests.get("http://127.0.0.1:8402/stats.json", timeout=6)
        f = r.json()["faucet"]
        left, bal = f.get("claims_remaining"), f.get("balance_xno")
    except Exception:
        return                      # RPC/service hiccup — never cry wolf
    if left is None or bal is None:
        return
    crossed = [m for m in LOW_MARKS if left <= m]
    mark = min(crossed) if crossed else None   # lowest mark, so it escalates
    if mark is None:
        st["low_mark"] = None       # healthy again after a top-up
        return
    if st.get("low_mark") == mark:
        return                      # already warned at this level
    st["low_mark"] = mark
    if mark == 0:
        tg("🏜️ <b>Feeless402 faucet is DRY</b>\n"
           "Claims are failing with 503 right now — agents that arrive will "
           "bounce.\nRefill: <code>nano-pay send &lt;faucet-addr&gt;</code> "
           "(address is in the 503 body and on /stats)")
    else:
        tg(f"🚰 <b>Feeless402 faucet running low</b>\n"
           f"About <b>{left}</b> claims left ({bal} XNO).\n"
           "Worth topping up before it empties — a dry faucet turns arriving "
           "agents away silently.")


def main() -> None:
    led = load(LEDGER, {"addresses": {}, "ips": {}})
    addrs = set(led.get("addresses", {}))
    ext_ips = {ip for ip in led.get("ips", {}) if ip not in LOCAL}
    ext_addrs = _external_addrs(led)

    st = load(STATE, None)
    if st is None:
        # First run: baseline existing claims silently, confirm the channel works.
        STATE.write_text(json.dumps(
            {"seen_addr": sorted(addrs), "seen_ext_addr": sorted(ext_addrs)}))
        tg(
            "✅ <b>Feeless402 faucet alerts armed</b>\n"
            "I'll ping you here the moment a real (external) agent claims "
            "starter Nano.\n"
            f"Baseline now: {len(addrs)} claim(s) — "
            "the next ping means a stranger showed up. 🎉\n"
            "Stats: https://feeless402.com/stats"
        )
        return

    seen_ext = set(st.get("seen_ext_addr", []))
    new_ext = ext_addrs - seen_ext

    if new_ext:  # a real external agent claimed
        total = len(addrs)
        disp = round(total * FAUCET_XNO, 6)
        newest = sorted(new_ext)[-1]
        extra = f" (+{len(new_ext) - 1} more)" if len(new_ext) > 1 else ""
        tg(
            "🚰🎉 <b>Feeless402 — real faucet claim!</b>\n"
            "A stranger's agent just pulled starter Nano.\n"
            f"New address{extra}: <code>{newest}</code>\n"
            f"Total agents funded: <b>{total}</b> · {disp} XNO dispensed\n"
            f"External IPs so far: {len(ext_ips)}\n"
            "See it live: https://feeless402.com/stats"
        )

    check_refill_needed(st)
    st.update({"seen_addr": sorted(addrs), "seen_ext_addr": sorted(ext_addrs)})
    STATE.write_text(json.dumps(st))


if __name__ == "__main__":
    main()
