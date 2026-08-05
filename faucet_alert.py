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


def main() -> None:
    led = load(LEDGER, {"addresses": {}, "ips": {}})
    addrs = set(led.get("addresses", {}))
    ips = set(led.get("ips", {}))
    ext_ips = {ip for ip in ips if ip not in LOCAL}

    st = load(STATE, None)
    if st is None:
        # First run: baseline existing claims silently, confirm the channel works.
        STATE.write_text(json.dumps(
            {"seen_addr": sorted(addrs), "seen_ext_ip": sorted(ext_ips)}))
        tg(
            "✅ <b>Feeless402 faucet alerts armed</b>\n"
            "I'll ping you here the moment a real (external) agent claims "
            "starter Nano.\n"
            f"Baseline now: {len(addrs)} claim(s), all local/test — "
            "so the next ping means a stranger showed up. 🎉\n"
            "Stats: https://feeless402.com/stats"
        )
        return

    seen_addr = set(st.get("seen_addr", []))
    seen_ext_ip = set(st.get("seen_ext_ip", []))
    new_ext_ip = ext_ips - seen_ext_ip
    new_addr = addrs - seen_addr

    if new_ext_ip:  # a real external agent claimed
        total = len(addrs)
        disp = round(total * FAUCET_XNO, 6)
        newest = sorted(new_addr)[-1] if new_addr else "(address n/a)"
        extra = f" (+{len(new_addr)} new)" if len(new_addr) > 1 else ""
        tg(
            "🚰🎉 <b>Feeless402 — real faucet claim!</b>\n"
            "A stranger's agent just pulled starter Nano.\n"
            f"New address{extra}: <code>{newest}</code>\n"
            f"Total agents funded: <b>{total}</b> · {disp} XNO dispensed\n"
            "See it live: https://feeless402.com/stats"
        )

    STATE.write_text(json.dumps(
        {"seen_addr": sorted(addrs), "seen_ext_ip": sorted(ext_ips)}))


if __name__ == "__main__":
    main()
