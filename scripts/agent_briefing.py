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
:root{--paper:#f3f5f7;--panel:#ffffff;--ink:#1c2420;--ink-soft:#55605a;
--line:#d8dedb;--accent:#0d8a58;--accent-ink:#0a6b45;--amber:#b97d10;
--term-bg:#182019;--term-ink:#d7e4da;--term-dim:#7d8f84;
--mono:ui-monospace,"SF Mono","Cascadia Code","JetBrains Mono",Menlo,Consolas,monospace;
--serif:Charter,"Iowan Old Style","Palatino Linotype",Georgia,serif}
:root[data-theme="light"]{--paper:#f3f5f7;--panel:#fff;--ink:#1c2420;
--ink-soft:#55605a;--line:#d8dedb;--accent:#0d8a58;--accent-ink:#0a6b45;
--amber:#b97d10;--term-bg:#182019;--term-ink:#d7e4da;--term-dim:#7d8f84}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);
font-family:var(--serif);font-size:17px;line-height:1.65}
main{max-width:44rem;margin:0 auto;padding:0 1.25rem 5rem}
header{padding:2.2rem 0 0;display:flex;align-items:baseline;
justify-content:space-between;gap:1rem;flex-wrap:wrap}
.brand{font-family:var(--mono);font-size:.95rem}
.brand a{color:inherit;text-decoration:none}.brand b{color:var(--accent-ink)}
nav{font-family:var(--mono);font-size:.78rem;display:flex;gap:1.1rem;flex-wrap:wrap}
a{color:var(--accent-ink);text-underline-offset:3px}
h1{font-size:1.9rem;font-weight:600;margin:2rem 0 .6rem;text-wrap:balance;line-height:1.25}
h2{font-size:1.25rem;font-weight:600;margin:2.2rem 0 .6rem}
p{margin:0 0 1rem}.dim{color:var(--ink-soft)}
.mono{font-family:var(--mono);font-size:.8rem;word-break:break-all}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;
padding:1rem 1.2rem;margin:1.2rem 0;overflow-x:auto}
.term{background:var(--term-bg);color:var(--term-ink);border-radius:8px;
border:1px solid var(--line);padding:.9rem 1.05rem;font-family:var(--mono);
font-size:.78rem;line-height:1.8;overflow-x:auto;margin:1.2rem 0}
.term .tdim{color:var(--term-dim)}.term a{color:var(--accent)}
.receipt{word-break:break-all}
.opinion{border-left:3px solid var(--accent);padding-left:1.3rem}
ul.arch{list-style:none;padding:0}ul.arch li{margin:.35rem 0}
details pre{background:var(--term-bg);color:var(--term-ink);border-radius:8px;
padding:.9rem 1.05rem;white-space:pre-wrap}
@media (max-width:640px){h1{font-size:1.55rem}h2{font-size:1.15rem}}
"""


def header_html(sub):
    return (f'<header><div class="brand"><a href="/"><b>feeless402</b></a> / {sub}</div>'
            '<nav><a href="/">Home</a><a href="/docs.html">Docs</a>'
            '<a href="/faucet.html">Faucet</a><a href="/stats">Stats</a>'
            '<a href="/briefing/">Briefing</a>'
            '<a href="/audits/">Audits</a>'
            '<a href="https://railhint.com" target="_blank" rel="noopener">Spec</a>'
            '<a href="/llms.txt">llms.txt</a>'
            '<a href="/.well-known/agent-card.json">A2A</a></nav></header>')

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


def day_page(date, headline, briefing, opinion, payments, data,
             prev_date=None, next_date=None):
    spent = sum(float(p["amount_xno"]) for p in payments if p["amount_xno"] != "?")
    prev_a = (f'<a href="/briefing/{prev_date}.html">← {prev_date}</a>'
              if prev_date else '<span class="dim">← (first issue)</span>')
    next_a = (f'<a href="/briefing/{next_date}.html">{next_date} →</a>'
              if next_date else '<span class="dim">(latest) →</span>')
    issue_nav = (f'<p class="dim mono" style="display:flex;justify-content:space-between;'
                 f'gap:1rem;flex-wrap:wrap">{prev_a} '
                 f'<a href="/briefing/">all briefings</a> {next_a}</p>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>x402 Daily Briefing — {date}</title>
<meta name="description" content="{html.escape(headline)}">
<style>{CSS}</style></head><body><main>
{header_html("briefing")}
<p class="dim mono" style="margin-top:2rem">{date} · x402 Daily Briefing</p>
<h1>{html.escape(headline)}</h1>
<div class="panel dim" style="font-size:.9rem">{DISCLOSURE}</div>
{paras(briefing)}
<h2>Opinion</h2>
<div class="opinion">{paras(opinion)}</div>
<h2>What today's writing cost</h2>
<div class="term">{receipts_html(payments)}
<div class="tdim" style="margin-top:.5rem">total {spent:.6f} XNO — fee-free, settled in under a second</div></div>
<details class="dim"><summary class="mono" style="cursor:pointer">raw data given to the model</summary>
<pre class="mono">{html.escape(json.dumps(data, indent=1, default=str)[:6000])}</pre></details>
<hr style="border:0;border-top:1px solid var(--line);margin:2rem 0 1rem">
{issue_nav}
<p class="dim mono"><a href="/briefing/feed.json">feed.json</a></p>
</main></body></html>"""


def _month_name(ym):
    return datetime.strptime(ym, "%Y-%m").strftime("%B %Y")


def index_page(days, totals):
    latest = days[-1] if days else None
    spent = totals["spent_xno"]
    avg = spent / totals["days"] if totals["days"] else 0
    first = days[0]["date"] if days else "—"

    tiles = "".join(
        f'<div class="tile"><div class="tnum">{v}</div><div class="tlab">{k}</div></div>'
        for k, v in [
            ("daily briefings", totals["days"]),
            ("paid inference calls", totals["calls"]),
            ("XNO spent, lifetime", f"{spent:.6f}"),
            ("≈ USD, lifetime", f"${spent * 0.4:.4f}"),
            ("avg per briefing", f"{avg:.4f} XNO"),
            ("network fees paid", "0"),
        ])

    months = {}
    for d in days:
        months.setdefault(d["date"][:7], []).append(d)
    sections = []
    for ym in sorted(months, reverse=True):
        rows = "\n".join(
            f'<li class="mono" data-s="{html.escape((d["date"] + " " + d["headline"]).lower())}">'
            f'<a href="/briefing/{d["date"]}.html">{d["date"]}</a> '
            f'— {html.escape(d["headline"])} '
            f'<span class="dim">({d["spent_xno"]} XNO)</span></li>'
            for d in reversed(months[ym]))
        sections.append(f'<section class="month"><h3>{_month_name(ym)} '
                        f'<span class="dim" style="font-weight:400">'
                        f'({len(months[ym])})</span></h3>'
                        f'<ul class="arch">{rows}</ul></section>')

    latest_link = (f'<p><a href="/briefing/{latest["date"]}.html">Read the latest '
                   f'briefing → {html.escape(latest["headline"])}</a></p>' if latest else "")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>x402 Daily Briefing</title>
<meta name="description" content="A daily x402 and agentic-payments briefing researched, written, and paid for entirely by an AI agent — feeless Nano (XNO) micropayments over x402 rails, block hashes on every page.">
<style>{CSS}
.agentic{{border-left:3px solid var(--accent);background:var(--panel);
border:1px solid var(--line);border-left:3px solid var(--accent);
border-radius:0 8px 8px 0;padding:.9rem 1.1rem;font-family:var(--mono);
font-size:.8rem;line-height:1.7;margin:1.4rem 0}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr));
gap:.8rem;margin:1.4rem 0}}
.tile{{background:var(--panel);border:1px solid var(--line);border-radius:8px;
padding:.8rem .9rem}}
.tnum{{font-family:var(--mono);font-size:1.15rem;font-weight:700;
color:var(--accent-ink);word-break:break-all}}
.tlab{{font-family:var(--mono);font-size:.66rem;letter-spacing:.1em;
text-transform:uppercase;color:var(--ink-soft);margin-top:.15rem}}
#q{{width:100%;font-family:var(--mono);font-size:.85rem;padding:.6rem .8rem;
border:1px solid var(--line);border-radius:8px;background:var(--panel);
color:var(--ink);margin:.4rem 0 .2rem}}
.month h3{{font-size:1.05rem;margin:1.8rem 0 .4rem}}
</style></head><body><main>
{header_html("briefing")}
<h1>x402 Daily Briefing</h1>
<div class="agentic"><b>100% agentic.</b> Every issue below was researched,
written, and <b>paid for by an AI agent with no human in the loop</b>. The
writing is LLM inference bought per-call from a third-party merchant over
<b>x402</b>, settled in <b>feeless Nano (XNO)</b> — zero network fees,
sub-second finality, self-custodied wallet. Every payment's block hash is
published on its page. No API keys. No subscription. No payment, no page.</div>
<div class="tiles">{tiles}</div>
{latest_link}
<h2>Archive</h2>
<input id="q" type="search" placeholder="Search briefings — date or headline…"
  oninput="var q=this.value.toLowerCase().trim();
document.querySelectorAll('ul.arch li').forEach(function(li){{
li.style.display=(!q||li.dataset.s.indexOf(q)>-1)?'':'none'}});
document.querySelectorAll('section.month').forEach(function(s){{
s.style.display=s.querySelectorAll('li:not([style*=none])').length?'':'none'}})">
{"".join(sections)}
<p class="dim">Publishing daily since {first}. Machine-readable:
<a href="/briefing/feed.json">feed.json</a> · How this works:
<a href="/">feeless402.com</a></p>
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
    try:
        # A top-up sent to this wallet is invisible to the balance check until
        # a receive block pockets it (the Aug 24 outage: 0.1 XNO sat pending
        # while the run died on "insufficient balance").
        got = wallet.receive_all(rpc, prework=True)
        if got:
            log(f"pocketed {len(got)} pending receivable(s)")
    except Exception as e:
        log(f"receive_all failed (continuing on current balance): {e}")
    text, payments = think(data, yesterday, wallet, rpc)
    log(f"paid writing received: {len(text)} chars, {len(payments)} payment(s)")
    headline, briefing, opinion = split_sections(text)

    spent = sum(float(p["amount_xno"]) for p in payments if p["amount_xno"] != "?")
    entry = {"date": today, "url": f"{SITE}/briefing/{today}.html",
             "headline": headline, "spent_xno": f"{spent:.6f}",
             "blocks": [p["block"] for p in payments if p.get("block")],
             "model": MODEL,
             "briefing_text": briefing, "opinion_text": opinion,
             "payments": payments, "data": data,
             "data_snapshot": {k: v for k, v in data.items()
                               if k in ("bazaar_total_resources", "agent_tools",
                                        "x402_list", "nanogpt_quote")}}
    state["days"].append(entry)
    state["totals"]["days"] += 1
    state["totals"]["calls"] += len(payments)
    state["totals"]["spent_xno"] = round(state["totals"]["spent_xno"] + spent, 8)

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1))
    render_all(state)  # today's page + refreshed neighbors, index, feed
    log(f"published {today}: {headline!r}, spent {spent:.6f} XNO")

    try:  # pre-cache next payment's PoW; harmless if it fails
        wallet.prework(wallet._work_root(wallet.synced_account(rpc)), rpc)
    except Exception as e:
        log("prework skipped:", e)


def render_all(state):
    """Write every day page (with prev/next links), the index, and the feed
    from stored state — no payment, no new text."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    days = state["days"]
    for i, d in enumerate(days):
        if "briefing_text" not in d:
            log(f"skip {d['date']}: no stored text (pre-restyle entry)")
            continue
        (OUT_DIR / f"{d['date']}.html").write_text(day_page(
            d["date"], d["headline"], d["briefing_text"], d["opinion_text"],
            d.get("payments") or [{"amount_xno": d["spent_xno"], "block": b,
                                   "model": d["model"], "merchant": "nano-gpt.com"}
                                  for b in d["blocks"]],
            d.get("data") or d.get("data_snapshot") or {},
            prev_date=days[i - 1]["date"] if i > 0 else None,
            next_date=days[i + 1]["date"] if i + 1 < len(days) else None))
    (OUT_DIR / "index.html").write_text(index_page(days, state["totals"]))
    feed = {"title": "x402 Daily Briefing", "site": f"{SITE}/briefing/",
            "generated_unix": int(time.time()), "totals": state["totals"],
            "days": [{k: v for k, v in d.items()
                      if k not in ("data_snapshot", "data",
                                   "briefing_text", "opinion_text")}
                     for d in days]}
    (OUT_DIR / "feed.json").write_text(json.dumps(feed, indent=1))
    log(f"rendered {len(days)} day page(s) + index + feed")


def rerender():
    render_all(json.loads(STATE.read_text()))


if __name__ == "__main__":
    try:
        rerender() if "--rerender" in sys.argv else main()
    except Exception as e:
        log(f"FATAL — no page published: {type(e).__name__}: {e}")
        sys.exit(1)
