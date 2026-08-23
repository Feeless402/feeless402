#!/usr/bin/env python3
"""Daily x402 / agentic-payments briefing, written by paid inference.

The demonstration is the architecture: research is free (public APIs and
402 quotes), but every word of the published briefing is written by LLM
inference bought per-call from a third-party merchant (NanoGPT) over
x402 on the feeless Nano rail. Receipts — block hashes — publish with
the text. If the agent cannot pay, there is no page: fail closed, never
fabricate a "paid" briefing.

Output: /var/www/feeless402/briefing/YYYY-MM-DD.html (one page per day)
        /var/www/feeless402/briefing/index.html (latest + archive)
        /var/www/feeless402/briefing/feed.json  (machine-readable)
State:  /root/nano-pay/logs/briefing-state.json (deltas + ledger)

Run daily from cron:  .venv/bin/python scripts/agent_briefing.py
Env: F402_AGENT_MODEL (default gpt-5-nano), F402_AGENT_BUDGET_XNO/day.
"""

import html
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from nano_pay import raw_to_xno, xno_to_raw
from nano_pay.rpc import RPC
from nano_pay.wallet import Wallet
from nano_pay.x402 import request_with_payment

OUT_DIR = Path("/var/www/feeless402/briefing")
STATE = Path("/root/nano-pay/logs/briefing-state.json")
NANOGPT = "https://nano-gpt.com/api/v1/chat/completions"
MODEL = os.environ.get("F402_AGENT_MODEL", "openai/gpt-5.6-luna")
BUDGET_RAW = xno_to_raw(os.environ.get("F402_AGENT_BUDGET_XNO", "0.12"))
PER_CALL_CAP_RAW = xno_to_raw(os.environ.get("F402_AGENT_CALL_CAP_XNO", "0.06"))
EXPLORER = "https://nanexplorer.com/nano/block/"
SITE = "https://feeless402.com"

today = datetime.now(timezone.utc).strftime("%Y-%m-%d")


def log(*a):
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}]", *a, flush=True)


# ---------------------------------------------------------------- gather (free)

def _get_json(url, timeout=20):
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def gather():
    """Collect today's public data. Tolerant: each source may fail alone."""
    data, errors = {}, {}

    try:  # x402 repo activity, last 24h (titles only — the model sees real items)
        out = subprocess.run(
            ["gh", "api", "--paginate",
             "repos/x402-foundation/x402/issues?state=all&sort=updated&direction=desc&per_page=50"],
            capture_output=True, text=True, timeout=60)
        items = json.loads(out.stdout) if out.returncode == 0 else []
        cutoff = time.time() - 86400
        recent = []
        for it in items:
            ts = datetime.fromisoformat(it["updated_at"].replace("Z", "+00:00")).timestamp()
            if ts < cutoff:
                continue
            recent.append({
                "number": it["number"],
                "title": it["title"][:120],
                "state": it["state"],
                "is_pr": "pull_request" in it,
                "author": it["user"]["login"],
                "comments": it.get("comments", 0),
            })
        data["x402_repo_last24h"] = recent[:25]
    except Exception as e:
        errors["x402_repo"] = str(e)

    try:  # CDP Bazaar catalog size (1 request)
        d = _get_json("https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources?limit=1")
        data["bazaar_total_resources"] = d["pagination"]["total"]
    except Exception as e:
        errors["bazaar"] = str(e)

    try:  # agent-tools.cloud directory stats
        d = _get_json("https://agent-tools.cloud/api/v1/stats")
        bc = d.get("by_chain") or {}
        data["agent_tools"] = {
            "total_services": d.get("total"),
            "healthy": d.get("healthy"),
            "chains": len(bc),
            "nano_entries": {k: v for k, v in bc.items() if "nano" in k.lower()},
        }
    except Exception as e:
        errors["agent_tools"] = str(e)

    try:  # x402-list directory stats
        d = _get_json("https://x402-list.com/api/v1/stats")
        data["x402_list"] = d.get("data", d)
    except Exception as e:
        errors["x402_list"] = str(e)

    try:  # live rail quote from NanoGPT (free dry-run — the 402 itself)
        r = requests.post(NANOGPT, headers={"x-x402": "true"}, timeout=30,
                          json={"model": MODEL,
                                "messages": [{"role": "user", "content": "hi"}]})
        if r.status_code == 402:
            from nano_pay.x402 import offer_amount_raw, parse_quote, pick_nano_offer
            q = parse_quote(r)
            offer = pick_nano_offer(q)
            data["nanogpt_quote"] = {
                "model": MODEL,
                "price_xno_per_call": raw_to_xno(offer_amount_raw(offer)),
                "rails_offered": sorted({(o.get("network") or "?")
                                         for o in (q.get("accepts") or [])}),
            }
        else:
            errors["nanogpt_quote"] = f"status {r.status_code}"
    except Exception as e:
        errors["nanogpt_quote"] = str(e)

    if errors:
        data["gaps"] = errors  # the model is told what's missing, never guesses
    return data


# ---------------------------------------------------------------- think (paid)

PROMPT = """You are the author of the daily briefing at feeless402.com/briefing — \
a short, factual, opinionated daily note on the x402 payment protocol ecosystem \
and agentic payments, read by both humans and AI agents. You are yourself an AI \
agent: this very inference call is being bought per-call from NanoGPT over x402, \
paid in feeless Nano (XNO), and the payment's block hash will be published under \
your text.

TODAY'S DATA (gathered in the last hour):
{data}

YESTERDAY'S NUMBERS (for deltas; may be empty on day one):
{yesterday}

Write the briefing with EXACTLY these three sections, each starting with its \
label on its own line:

HEADLINE: one factual line, under 90 characters.

BRIEFING: two to four short paragraphs. What changed in the last 24 hours — \
specific issues/PRs by number and title, catalog growth with real numbers, \
rail prices. Only state facts present in the data above; where a source is in \
"gaps", say it is unavailable today rather than guessing. Numbers you cite \
must appear in the data.

OPINION: one paragraph, clearly your own analysis: a stance, an implication, \
or a falsifiable expectation that follows from the data. Direct, no hedging \
boilerplate.

Style: plain text paragraphs, American spelling, no marketing language, no \
exclamation marks, no bullet lists."""


def think(data, yesterday, wallet, rpc):
    """Buy the day's writing from NanoGPT over x402. Returns (text, payments)."""
    payments = []
    spent = 0
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT.format(
            data=json.dumps(data, indent=1, default=str)[:9000],
            yesterday=json.dumps(yesterday, indent=1, default=str)[:2000])}],
    }
    if spent + PER_CALL_CAP_RAW > BUDGET_RAW:
        raise RuntimeError("daily budget would be exceeded before first call")
    r, receipt = request_with_payment(
        "POST", NANOGPT, wallet, rpc, max_raw=PER_CALL_CAP_RAW,
        prework=False, json=body,
    )
    if r.status_code != 200:
        raise RuntimeError(f"paid call failed: HTTP {r.status_code} {r.text[:200]}")
    if not receipt:
        raise RuntimeError("no payment was made — refusing to publish unpaid text")
    # Our just-published send block is now the wallet's frontier.
    frontier = (rpc.account_info(wallet.address) or {}).get("frontier", "")
    amount = receipt.get("amount_xno") or "?"
    payments.append({"amount_xno": str(amount), "block": frontier, "model": MODEL,
                     "merchant": "nano-gpt.com"})
    out = r.json()
    text = out["choices"][0]["message"]["content"]
    if not text or len(text.strip()) < 200:
        raise RuntimeError(f"model returned {len(text or '')} chars — not publishable")
    return text, payments


def split_sections(text):
    m = {}
    cur = None
    for line in text.splitlines():
        s = line.strip()
        head = re.match(r"^(HEADLINE|BRIEFING|OPINION)\s*:?\s*(.*)$", s)
        if head:
            cur = head.group(1)
            m[cur] = [head.group(2)] if head.group(2) else []
        elif cur:
            m[cur].append(line)
    if not all(k in m for k in ("HEADLINE", "BRIEFING", "OPINION")):
        raise RuntimeError(f"model output missing sections; got {list(m)}")
    return ("\n".join(m["HEADLINE"]).strip(),
            "\n".join(m["BRIEFING"]).strip(),
            "\n".join(m["OPINION"]).strip())


# ---------------------------------------------------------------- publish

CSS = """
:root{--bg:#0b0f14;--fg:#d8e1ea;--dim:#7b8a99;--acc:#37c8ab;--card:#121924;
--mono:ui-monospace,'SF Mono',Menlo,Consolas,monospace}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.65 system-ui,-apple-system,Segoe UI,sans-serif;padding:2rem 1rem}
main{max-width:44rem;margin:0 auto}a{color:var(--acc)}h1{font-size:1.5rem;
line-height:1.3}h2{font-size:1.05rem;margin-top:2rem}.dim{color:var(--dim)}
.mono{font-family:var(--mono);font-size:.85rem}.card{background:var(--card);
border-radius:8px;padding:1rem 1.2rem;margin:1rem 0;overflow-x:auto}
.receipt{font-family:var(--mono);font-size:.78rem;word-break:break-all}
nav{font-family:var(--mono);font-size:.78rem;display:flex;gap:1.1rem;
flex-wrap:wrap;margin-bottom:2rem}nav a{text-decoration:none}
.opinion{border-left:3px solid var(--acc);padding-left:1rem}
ul.arch{list-style:none;padding:0}ul.arch li{margin:.25rem 0}
"""

NAV = ('<nav><a href="/">Home</a><a href="/briefing/">Briefing</a>'
       '<a href="/docs.html">Docs</a><a href="/faucet.html">Faucet</a>'
       '<a href="/stats">Stats</a><a href="/llms.txt">llms.txt</a></nav>')

DISCLOSURE = (
    'Researched from public APIs for free; <b>written by inference bought '
    'per-call over x402</b> on the feeless Nano rail from '
    '<a href="https://nano-gpt.com" target="_blank" rel="noopener noreferrer">'
    'NanoGPT</a>, a third-party merchant. Receipts below — every word here was '
    'paid for on-chain before it existed.')


def paras(text, cls=""):
    c = f' class="{cls}"' if cls else ""
    return "\n".join(f"<p{c}>{html.escape(p.strip())}</p>"
                     for p in re.split(r"\n\s*\n", text) if p.strip())


def receipts_html(payments):
    rows = []
    for p in payments:
        link = (f'<a href="{EXPLORER}{p["block"]}" target="_blank" '
                f'rel="noopener noreferrer">{p["block"][:16]}…</a>'
                if p.get("block") else "(hash unavailable)")
        rows.append(f'<div class="receipt">{html.escape(str(p["amount_xno"]))} XNO '
                    f'→ {html.escape(p["merchant"])} ({html.escape(p["model"])}) '
                    f'· block {link}</div>')
    return "\n".join(rows)


def day_page(date, headline, briefing, opinion, payments, data):
    spent = sum(float(p["amount_xno"]) for p in payments if p["amount_xno"] != "?")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>x402 Daily Briefing — {date}</title>
<meta name="description" content="{html.escape(headline)}">
<style>{CSS}</style></head><body><main>
{NAV}
<p class="dim mono">{date} · x402 Daily Briefing · feeless402.com</p>
<h1>{html.escape(headline)}</h1>
<div class="card dim" style="font-size:.85rem">{DISCLOSURE}</div>
{paras(briefing)}
<h2>Opinion</h2>
<div class="opinion">{paras(opinion)}</div>
<h2>What today's writing cost</h2>
<div class="card">{receipts_html(payments)}
<div class="dim mono" style="margin-top:.5rem">total {spent:.6f} XNO — fee-free, settled in under a second</div></div>
<details class="dim"><summary class="mono">raw data given to the model</summary>
<div class="card"><pre class="mono" style="white-space:pre-wrap">{html.escape(json.dumps(data, indent=1, default=str)[:6000])}</pre></div></details>
<p class="dim mono"><a href="/briefing/">← all briefings</a> · <a href="/briefing/feed.json">feed.json</a></p>
</main></body></html>"""


def index_page(days, totals):
    latest = days[-1] if days else None
    items = "\n".join(
        f'<li class="mono"><a href="/briefing/{d["date"]}.html">{d["date"]}</a> '
        f'— {html.escape(d["headline"])} '
        f'<span class="dim">({d["spent_xno"]} XNO)</span></li>'
        for d in reversed(days))
    latest_link = (f'<p><a href="/briefing/{latest["date"]}.html">Read today\'s '
                   f'briefing → {html.escape(latest["headline"])}</a></p>' if latest else "")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>x402 Daily Briefing</title>
<meta name="description" content="A daily x402 and agentic-payments briefing written by an AI agent that pays for its own inference with feeless Nano micropayments. Receipts on every page.">
<style>{CSS}</style></head><body><main>
{NAV}
<h1>x402 Daily Briefing</h1>
<p>A daily note on the x402 protocol ecosystem and agentic payments, published
by an AI agent that <b>buys its own inference per-call over x402</b> — feeless
Nano micropayments to a third-party merchant, block hashes published with every
page. No API keys, no subscription, no human in the writing loop.</p>
<div class="card mono dim">lifetime: {totals["days"]} briefings ·
{totals["calls"]} paid inference calls · {totals["spent_xno"]:.6f} XNO spent ·
≈ ${totals["spent_xno"] * 0.4:.4f} at recent rates</div>
{latest_link}
<h2>Archive</h2>
<ul class="arch">{items}</ul>
<p class="dim">Machine-readable: <a href="/briefing/feed.json">feed.json</a> ·
How this works: <a href="/">feeless402.com</a> · The agent cannot publish
without paying first — no payment, no page.</p>
</main></body></html>"""


def main():
    log(f"briefing run for {today}, model {MODEL}")
    state = json.loads(STATE.read_text()) if STATE.exists() else {"days": [], "totals": {"days": 0, "calls": 0, "spent_xno": 0.0}}
    if any(d["date"] == today for d in state["days"]):
        log("already published today — nothing to do")
        return

    data = gather()
    log(f"gathered: {list(data)}")
    yesterday = state["days"][-1].get("data_snapshot", {}) if state["days"] else {}

    wallet = Wallet().load()
    rpc = RPC()
    text, payments = think(data, yesterday, wallet, rpc)
    log(f"paid writing received: {len(text)} chars, {len(payments)} payment(s)")
    headline, briefing, opinion = split_sections(text)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{today}.html").write_text(
        day_page(today, headline, briefing, opinion, payments, data))

    spent = sum(float(p["amount_xno"]) for p in payments if p["amount_xno"] != "?")
    entry = {"date": today, "url": f"{SITE}/briefing/{today}.html",
             "headline": headline, "spent_xno": f"{spent:.6f}",
             "blocks": [p["block"] for p in payments if p.get("block")],
             "model": MODEL,
             "data_snapshot": {k: v for k, v in data.items()
                               if k in ("bazaar_total_resources", "agent_tools",
                                        "x402_list", "nanogpt_quote")}}
    state["days"].append(entry)
    state["totals"]["days"] += 1
    state["totals"]["calls"] += len(payments)
    state["totals"]["spent_xno"] = round(state["totals"]["spent_xno"] + spent, 8)

    (OUT_DIR / "index.html").write_text(index_page(state["days"], state["totals"]))
    feed = {"title": "x402 Daily Briefing", "site": f"{SITE}/briefing/",
            "generated_unix": int(time.time()), "totals": state["totals"],
            "days": [{k: v for k, v in d.items() if k != "data_snapshot"}
                     for d in state["days"]]}
    (OUT_DIR / "feed.json").write_text(json.dumps(feed, indent=1))
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1))
    log(f"published {today}: {headline!r}, spent {spent:.6f} XNO")

    try:  # pre-cache next payment's PoW; harmless if it fails
        wallet.prework(wallet._work_root(wallet.synced_account(rpc)), rpc)
    except Exception as e:
        log("prework skipped:", e)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL — no page published: {type(e).__name__}: {e}")
        sys.exit(1)
