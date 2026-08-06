"""Feeless402 server — earn feeless XNO micropayments on any endpoint.

Every 402 this server emits is also an upsell: alongside the payment
menu it ships `extensions.railHint` — machine-readable instructions that
teach a visiting agent how to bootstrap onto the feeless rail (install
client, top up or hit the faucet, retry). The 402 IS the marketing.

Run:  nano-pay serve [--port 8402]
Env:  F402_SITE_URL, F402_DOCS_URL, F402_PRICE_XNO, F402_FAUCET_XNO,
      F402_FAUCET_PER_IP_PER_DAY, NANO_PAY_HOME
"""

import base64
import json
import os
import threading
import time
from pathlib import Path

import anyio

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from . import __version__, raw_to_xno, xno_to_raw
from .rpc import RPC
from .verify import PaymentInvalid, settle_block, verify_block
from .wallet import DEFAULT_DIR, Wallet

SITE_URL = os.environ.get("F402_SITE_URL", "https://feeless402.com")
DOCS_URL = os.environ.get(
    "F402_DOCS_URL", "https://github.com/feeless402/feeless402"
)
PRICE_XNO = os.environ.get("F402_PRICE_XNO", "0.0001")
FAUCET_XNO = os.environ.get("F402_FAUCET_XNO", "0.005")
FAUCET_PER_IP_PER_DAY = int(os.environ.get("F402_FAUCET_PER_IP_PER_DAY", "3"))
FAUCET_POW = os.environ.get("F402_FAUCET_POW", "0") == "1"
# Sibling faucets advertised in railHint — the federation list. Comma-sep.
FAUCET_FEDERATION = [
    u for u in os.environ.get(
        "F402_FAUCETS", "https://feeless402.com"
    ).split(",") if u
]

rpc = RPC()


def _wallet(name: str) -> Wallet:
    w = Wallet(DEFAULT_DIR / f"{name}-wallet.json")
    if not w.exists():
        w.create()
    else:
        w.load()
    return w


server_wallet = _wallet("server")
faucet_wallet = _wallet("faucet")
demo_wallet = _wallet("demo")  # pays the homepage live demo; auto-refilled from the faucet wallet

FAUCET_LEDGER = DEFAULT_DIR / "faucet-ledger.json"

# The faucet wallet is spent from the faucet endpoint AND the demo-refill
# threads; concurrent sends would fork on the same frontier.
FAUCET_LOCK = threading.Lock()


def _ledger():
    if FAUCET_LEDGER.exists():
        return json.loads(FAUCET_LEDGER.read_text())
    return {"addresses": {}, "ips": {}}


def _save_ledger(led):
    tmp = FAUCET_LEDGER.with_name(FAUCET_LEDGER.name + ".tmp")
    tmp.write_text(json.dumps(led, indent=2))
    os.replace(tmp, FAUCET_LEDGER)


PAID_CALLS = DEFAULT_DIR / "paid-calls.json"


def _self_addresses() -> set:
    """Our own Nano addresses — main + test wallets, outside the repo."""
    try:
        return {l.strip() for l in
                (DEFAULT_DIR / "self-addresses.txt").read_text().splitlines()
                if l.strip() and not l.startswith("#")}
    except Exception:
        return set()


def _count_paid_call(payer: str, demo_address: str) -> None:
    """Tally a settled payment by who paid. Never raises — a bookkeeping
    failure must not affect a payment that already settled on-chain."""
    try:
        if payer == demo_address:
            bucket = "demo"          # our homepage unlock button
        elif payer in _self_addresses():
            bucket = "self"          # our own test wallets
        else:
            bucket = "external"      # a stranger actually paid us
        try:
            d = json.loads(PAID_CALLS.read_text())
        except Exception:
            d = {}
        d[bucket] = int(d.get(bucket, 0)) + 1
        d["last_unix"] = time.time()
        if bucket == "external":
            d["last_external_unix"] = time.time()
        tmp = PAID_CALLS.with_name(PAID_CALLS.name + ".tmp")
        tmp.write_text(json.dumps(d))
        os.replace(tmp, PAID_CALLS)
    except Exception:
        pass


def _faucet_funding() -> dict:
    """Who has put XNO INTO the faucet, read straight off the chain.

    Three kinds of deposit: our own top-ups, grants an agent didn't spend
    and sent back, and donations from people who owed us nothing.
    """
    try:
        hist = (rpc.call({"action": "account_history",
                          "account": faucet_wallet.address,
                          "count": "500"}).get("history") or [])
    except Exception:
        return {"donations": None, "donated_xno": None,
                "returned_grants": None, "recent": [], "address": None}
    ours = _self_addresses()
    claimants = set(_ledger().get("addresses", {}))
    donations, returned, donated_raw = [], 0, 0
    for e in hist:
        if e.get("type") != "receive":
            continue
        src, raw = e.get("account", ""), int(e.get("amount", 0))
        if src in claimants:            # an agent handing back what it didn't use
            if src not in ours:
                returned += 1
        elif src in ours:               # our own funding
            continue
        else:
            donated_raw += raw
            donations.append({"from": src, "xno": str(raw_to_xno(raw)),
                              "unix": int(e.get("local_timestamp") or 0)})
    donations.sort(key=lambda d: d["unix"], reverse=True)
    return {"donations": len(donations),
            # Distinct addresses, not deposits — one person topping up three
            # times is one person. (Someone using two addresses still counts
            # twice; the chain can't tell us otherwise.)
            "donors": len({d["from"] for d in donations}),
            "donated_xno": float(raw_to_xno(donated_raw)) if donated_raw else 0.0,
            "returned_grants": returned,
            "recent": donations[:8],
            "address": faucet_wallet.address}


def _paid_calls() -> dict:
    try:
        d = json.loads(PAID_CALLS.read_text())
        return {"external": int(d.get("external", 0)),
                "demo": int(d.get("demo", 0)),
                "self_test": int(d.get("self", 0)),
                "last_external_unix": d.get("last_external_unix")}
    except Exception:
        return {"external": None, "demo": None, "self_test": None,
                "last_external_unix": None}


def _self_ips() -> set:
    """Operator self-traffic IPs — loopback plus ~/.nano-pay/self-ips.txt."""
    ips = {"127.0.0.1", "::1", "localhost", "unknown", ""}
    try:
        ips |= {l.strip() for l in
                (DEFAULT_DIR / "self-ips.txt").read_text().splitlines()
                if l.strip() and not l.startswith("#")}
    except Exception:
        pass
    return ips


def _external_claim_times(led) -> list:
    """Claim timestamps attributable to non-operator IPs (matched ±2s)."""
    self_ips = _self_ips()
    ext = [t for ip, ts in led.get("ips", {}).items()
           if ip not in self_ips for t in ts]
    return sorted(at for at in (float(t) for t in
                                led.get("addresses", {}).values())
                  if any(abs(at - t) < 2 for t in ext))


_BLOCK_KEYS = {"type", "account", "previous", "representative",
               "balance", "link", "signature"}


def _block_shape_ok(block) -> bool:
    """Cheap structural check so malformed blocks never reach the RPC."""
    return isinstance(block, dict) and _BLOCK_KEYS <= set(block)


def _client_ip(request) -> str:
    """Real client IP behind the nginx proxy (X-Real-IP), with fallbacks."""
    return (request.headers.get("x-real-ip")
            or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown"))


def rail_hint(price_raw: int) -> dict:
    return {
        "cheapest": "nano:mainnet",
        "why": (
            "Sure, we could settle this over Base/Solana USDC and both pay "
            "gas + a 0.001-USDC price floor — or we settle in XNO: zero "
            "network fees, no minimum, sub-second finality. This quote is "
            f"the true metered price ({raw_to_xno(price_raw)} XNO)."
        ),
        "docs": DOCS_URL,
        "site": SITE_URL,
        "agentReadme": f"{SITE_URL}/llms.txt",
        "bootstrap": (
            "pip install feeless402 && nano-pay init && "
            f"nano-pay claim {SITE_URL}  # free starter XNO, then retry me"
        ),
        "topup": (
            "nano-pay topup 5 --asset USDC-BASE --execute  "
            "# $5 buys hundreds of thousands of micro-calls; "
            "any instant-swap service works"
        ),
        "faucets": FAUCET_FEDERATION,
        "spec": "x402 exact scheme on nano:mainnet — see x402nano.org",
    }


def payment_required_body(price_raw: int, pay_to: str, resource: str) -> dict:
    return {
        "x402Version": 2,
        "error": "Payment required — feeless XNO option available (see extensions.railHint)",
        "resource": {"url": resource, "mimeType": "application/json"},
        "accepts": [
            {
                "scheme": "exact",
                "network": "nano:mainnet",
                "asset": "XNO",
                "amount": str(price_raw),
                "payTo": pay_to,
                "maxTimeoutSeconds": 60,
            }
        ],
        "extensions": {"railHint": rail_hint(price_raw)},
    }


# ---------- live usage stats ----------
_STATS_CACHE = {}


def _cached(key, ttl, fn, fail_ttl=300):
    """Cache fn() for ttl seconds; on failure keep the last good value but
    retry after fail_ttl instead of a full ttl (one transient upstream 429
    after the daily reboot used to blank a stat for six hours)."""
    now = time.time()
    hit = _STATS_CACHE.get(key)
    if hit and now - hit[0] < hit[2]:
        return hit[1]
    try:
        val = fn()
        _STATS_CACHE[key] = (now, val, ttl)
    except Exception:
        val = hit[1] if hit else None
        _STATS_CACHE[key] = (now, val, fail_ttl)
    return val


def _http_json(url, timeout=6, retries=1):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "feeless402-stats"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if attempt < retries and e.code in (429, 502, 503):
                time.sleep(2)
                continue
            raise


def _pypi_downloads():
    d = _http_json(
        "https://pypistats.org/api/packages/feeless402/recent"
    ).get("data", {})
    return {"last_day": d.get("last_day"), "last_week": d.get("last_week"),
            "last_month": d.get("last_month")}


def _github_stats():
    d = _http_json("https://api.github.com/repos/Feeless402/feeless402")
    return {"stars": d.get("stargazers_count"), "forks": d.get("forks_count")}


def _clawhub_installs():
    d = _http_json("https://clawhub.ai/api/v1/search?q=nano-pay")
    for r in d.get("results", []):
        tag = (r.get("canonicalUrl", "") + r.get("displayName", "")).lower()
        if "nano-pay" in tag:
            m = r.get("metrics") or {}
            return {"downloads": r.get("downloads"),
                    "installs_60d": m.get("rolling60DayInstalls")}
    return {"downloads": None, "installs_60d": None}


def _stats_data():
    led = _ledger()
    claims = led.get("addresses", {})
    ts_list = sorted(float(t) for t in claims.values())
    day_ago = time.time() - 86400

    def _bal(w):
        try:
            return round(float(raw_to_xno(w.synced_account(rpc).raw_bal)), 6)
        except Exception:
            return None

    faucet_bal = _cached("faucet_bal", 300, lambda: _bal(faucet_wallet))
    per_claim = float(FAUCET_XNO)
    ext_times = _external_claim_times(led)
    return {
        "faucet": {
            # headline counts exclude the operator's own test claims
            "claims_total": len(ext_times),
            "claims_24h": sum(1 for t in ext_times if t > day_ago),
            "self_test_claims": len(claims) - len(ext_times),
            "xno_dispensed": round(len(claims) * per_claim, 6),
            "xno_per_claim": per_claim,
            "balance_xno": faucet_bal,
            "claims_remaining": (
                int(faucet_bal / per_claim) if faucet_bal else None
            ),
            "funding": _cached("faucet_funding", 300, _faucet_funding),
            "last_claim_unix": ts_list[-1] if ts_list else None,
            "recent_unix": ts_list[-60:],
        },
        "treasury": {"balance_xno": _cached(
                         "treasury_bal", 300, lambda: _bal(server_wallet)),
                     "receivable_xno": _cached(
                         "treasury_recv", 300,
                         lambda: round(float(raw_to_xno(sum(
                             int(a) for a in rpc.receivable(
                                 server_wallet.address).values()))), 6)),
                     "price_per_call_xno": float(PRICE_XNO),
                     # Paid calls split by who paid: strangers vs our own
                     # homepage demo vs our test wallets. The treasury
                     # balance alone reads as revenue when it mostly isn't.
                     "paid_calls": _paid_calls()},
        "downloads": {
            "pypi": _cached("pypi", 21600, _pypi_downloads),
            "github": _cached("github", 3600, _github_stats),
            "clawhub": _cached("clawhub", 3600, _clawhub_installs),
        },
        "links": {
            "site": SITE_URL, "pypi": "https://pypi.org/project/feeless402/",
            "mcp_registry": "com.feeless402/nano-pay", "repo": DOCS_URL,
        },
        "generated_unix": time.time(),
    }


def _extract_block(request: Request):
    hdr = request.headers.get("payment-signature") or request.headers.get(
        "x-payment"
    )
    if not hdr:
        return None
    try:
        payload = json.loads(base64.b64decode(hdr))
        return payload["payload"]["block"]
    except Exception:
        raise PaymentInvalid("unparseable payment header")


STATS_HTML = """<title>Stats — Feeless402</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --paper: #f3f5f7; --panel: #ffffff; --ink: #1c2420; --ink-soft: #55605a;
    --line: #d8dedb; --accent: #0d8a58; --accent-ink: #0a6b45; --amber: #b97d10;
    --term-bg: #182019; --term-ink: #d7e4da; --term-dim: #7d8f84;
    --mono: ui-monospace, "SF Mono", "Cascadia Code", "JetBrains Mono", Menlo, Consolas, monospace;
    --serif: Charter, "Iowan Old Style", "Palatino Linotype", Georgia, serif;
  }
  /* light mode enforced — identical page for every visitor */
  :root { color-scheme: light only; }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--paper); color: var(--ink); font-family: var(--serif); font-size: 17px; line-height: 1.65; }
  main { max-width: 44rem; margin: 0 auto; padding: 0 1.25rem 5rem; }
  header { padding: 2.2rem 0 0; display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
  .brand { font-family: var(--mono); font-size: .95rem; }
  .brand a { color: inherit; text-decoration: none; }
  .brand b { color: var(--accent-ink); }
  nav { font-family: var(--mono); font-size: .78rem; display: flex; gap: 1.1rem; flex-wrap: wrap; }
  a { color: var(--accent-ink); text-underline-offset: 3px; }
  h1 { font-size: 1.9rem; font-weight: 600; margin: 2rem 0 .4rem; text-wrap: balance; }
  h2 { font-size: 1.1rem; font-weight: 600; margin: 2.3rem 0 .2rem; }
  p { margin: 0 0 1rem; }
  .lede { color: var(--ink-soft); font-size: 1rem; }
  .upd { font-family: var(--mono); font-size: .72rem; color: var(--ink-soft); }
  .grid { display: grid; gap: .9rem; grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr)); margin: .9rem 0 0; }
  .tile { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 1rem 1.1rem; }
  .num { font-family: var(--mono); font-size: 1.85rem; font-weight: 700; color: var(--accent-ink); letter-spacing: -.02em; line-height: 1.1; }
  .lbl { font-size: .82rem; color: var(--ink-soft); margin-top: .35rem; }
  .foot { font-family: var(--mono); font-size: .74rem; color: var(--ink-soft); margin-top: 2.2rem; line-height: 1.9; }
  .days { display: grid; gap: .3rem; margin: .9rem 0 0; font-family: var(--mono); font-size: .78rem; }
  .drow { display: grid; grid-template-columns: 4.2rem 3.4rem 1fr; align-items: center; gap: .6rem; }
  .drow .dt { color: var(--ink-soft); }
  .drow .dv { text-align: right; font-weight: 700; }
  .dbar { height: 10px; border-radius: 0 4px 4px 0; background: var(--accent); min-width: 2px; }
  .cap { font-size: .8rem; color: var(--ink-soft); margin: .2rem 0 0; }
</style>
<main>
  <header>
    <div class="brand"><a href="/"><b>feeless402</b></a> / stats</div>
    <nav><a href="/">Home</a><a href="/docs.html">Docs</a><a href="/faucet.html">Faucet</a><a href="/stats">Stats</a><a href="https://railhint.com" target="_blank" rel="noopener">Spec</a><a href="/llms.txt">llms.txt</a></nav>
  </header>

  <h1>Live usage</h1>
  <p class="lede">Agents onboarding onto the feeless Nano rail &mdash; faucet claims, installs, and settlement. <span class="upd" id="ts">loading&hellip;</span></p>
  <p class="upd">Launched <b>August 2, 2026</b> &middot; numbers are climbing from zero as agents discover it &mdash; check back.</p>

  <h2>Faucet &mdash; agents funded</h2>
  <div class="grid">
    <div class="tile"><div class="num" id="f_total">&ndash;</div><div class="lbl">agents funded (all time, excl. our own tests)</div></div>
    <div class="tile"><div class="num" id="f_24h">&ndash;</div><div class="lbl">claims, last 24h</div></div>
    <div class="tile"><div class="num" id="f_disp">&ndash;</div><div class="lbl">XNO dispensed</div></div>
    <div class="tile"><div class="num" id="f_rem">&ndash;</div><div class="lbl">claims left in faucet</div></div>
  </div>

  <h2>Who keeps the faucet full</h2>
  <div class="grid">
    <div class="tile"><div class="num" id="c_don">&ndash;</div><div class="lbl">donations from the community</div></div>
    <div class="tile"><div class="num" id="c_xno">&ndash;</div><div class="lbl">XNO donated</div></div>
    <div class="tile"><div class="num" id="c_ret">&ndash;</div><div class="lbl">grants handed back unspent</div></div>
  </div>
  <p class="cap" id="c_thanks">&nbsp;</p>
  <div id="c_list"></div>
  <p class="cap">Faucet address &mdash; anything sent here becomes starter XNO for
  someone else's agent:<br><code id="c_addr" style="word-break:break-all"></code></p>

  <h2>Installs &amp; downloads</h2>
  <div class="grid">
    <div class="tile"><div class="num" id="d_pypi">&ndash;</div><div class="lbl">PyPI downloads (30d)</div></div>
    <div class="tile"><div class="num" id="d_pypi_d">&ndash;</div><div class="lbl">PyPI (last day)</div></div>
    <div class="tile"><div class="num" id="d_claw">&ndash;</div><div class="lbl">ClawHub downloads</div></div>
    <div class="tile"><div class="num" id="d_gh">&ndash;</div><div class="lbl">GitHub stars</div></div>
  </div>

  <h2>pip installs by day</h2>
  <p class="cap">Real installs via pypistats &mdash; mirror/bot downloads excluded. PyPI publishes each day with about a one-day lag, so today&rsquo;s row appears tomorrow.</p>
  <div class="days" id="pipdays"><span class="cap">loading&hellip;</span></div>

  <h2>Site visitors by day</h2>
  <p class="cap">Unique addresses on feeless402.com, common crawler noise filtered. Today&rsquo;s bar is still filling.</p>
  <div class="days" id="visdays"><span class="cap">loading&hellip;</span></div>

  <h2>Repository &mdash; last 14 days</h2>
  <div class="grid">
    <div class="tile"><div class="num" id="g_views">&ndash;</div><div class="lbl">repo views (14d)</div></div>
    <div class="tile"><div class="num" id="g_vuniq">&ndash;</div><div class="lbl">unique repo visitors</div></div>
    <div class="tile"><div class="num" id="g_clones">&ndash;</div><div class="lbl">clones (14d)</div></div>
    <div class="tile"><div class="num" id="g_forks">&ndash;</div><div class="lbl">forks</div></div>
  </div>

  <h2>Settlement</h2>
  <div class="grid">
    <div class="tile"><div class="num" id="p_ext">&ndash;</div><div class="lbl">paid calls by strangers</div></div>
    <div class="tile"><div class="num" id="p_demo">&ndash;</div><div class="lbl">homepage demo unlocks (we pay those)</div></div>
    <div class="tile"><div class="num" id="t_bal">&ndash;</div><div class="lbl">treasury balance (XNO, incl. our own demo)</div></div>
    <div class="tile"><div class="num" id="t_price">&ndash;</div><div class="lbl">price per call (XNO)</div></div>
  </div>

  <p class="foot" id="foot"></p>
</main>
<script>
function n(x){return x==null?'–':Number(x).toLocaleString()}
function xno(x){return x==null?'–':Number(x).toLocaleString(undefined,{maximumFractionDigits:5})}
function n2(id,v){document.getElementById(id).textContent=n(v)}
async function load(){
 try{
  const s=await (await fetch('/stats.json',{cache:'no-store'})).json();
  const f=s.faucet||{},d=s.downloads||{},t=s.treasury||{};
  n2('f_total',f.claims_total);n2('f_24h',f.claims_24h);
  document.getElementById('f_disp').textContent=xno(f.xno_dispensed);
  n2('f_rem',f.claims_remaining);
  const p=d.pypi||{},c=d.clawhub||{},g=d.github||{};
  n2('d_pypi',p.last_month);n2('d_pypi_d',p.last_day);
  n2('d_claw',c.downloads!=null?c.downloads:c.installs_60d);n2('d_gh',g.stars);
  var fu=f.funding||{};
  n2('c_don',fu.donors);n2('c_xno',fu.donated_xno);n2('c_ret',fu.returned_grants);
  document.getElementById('c_addr').textContent=fu.address||'–';
  var th=document.getElementById('c_thanks');
  th.innerHTML=(fu.donors>0)?'<strong>Thank you.</strong>':'';
  var cl=document.getElementById('c_list');
  cl.innerHTML=(fu.recent||[]).map(function(d){
    var when=d.unix?new Date(d.unix*1000).toLocaleDateString():'';
    return '<div class="cap"><a href="https://blocklattice.io/account/'+d.from+
      '" target="_blank" rel="noopener noreferrer">'+
      d.from.slice(0,16)+'…'+d.from.slice(-6)+'</a> &mdash; <strong>'+
      d.xno+' XNO</strong> '+when+'</div>';}).join('');
  var pc=t.paid_calls||{};n2('p_ext',pc.external);n2('p_demo',pc.demo);
  document.getElementById('t_bal').textContent=(t.balance_xno==null&&t.receivable_xno==null)?'–':xno((t.balance_xno||0)+(t.receivable_xno||0));
  document.getElementById('t_price').textContent=xno(t.price_per_call_xno);
  const gen=new Date((s.generated_unix||Date.now()/1000)*1000);
  document.getElementById('ts').textContent='updated '+gen.toLocaleTimeString();
  const lc=f.last_claim_unix?new Date(f.last_claim_unix*1000).toLocaleString():'none yet';
  document.getElementById('foot').innerHTML='Last faucet claim: '+lc+' &middot; <a href="/stats.json">raw JSON</a> &middot; <a href="https://pypi.org/project/feeless402/" target="_blank" rel="noopener noreferrer">PyPI</a> &middot; MCP registry: com.feeless402/nano-pay';
 }catch(e){document.getElementById('ts').textContent='stats unavailable';}
}
load();setInterval(load,30000);
async function loadCommunity(){
 try{
  const c=await (await fetch('/community-stats.json',{cache:'no-store'})).json();
  const bars=(id,rows,key)=>{const el=document.getElementById(id);
   if(!rows||!rows.length){el.innerHTML='<span class="cap">no data yet &mdash; check back</span>';return}
   const max=Math.max(...rows.map(r=>r[key]||0),1);
   el.innerHTML=rows.map(r=>'<div class="drow"><span class="dt">'+r.date.slice(5)+'</span><span class="dv">'+n(r[key])+'</span><div class="dbar" style="width:'+Math.max(2,(r[key]||0)/max*100)+'%"></div></div>').join('');};
  bars('pipdays',c.pypi_daily,'installs');
  bars('visdays',c.visitors,'uniques');
  const g=c.github||{};
  n2('g_views',g.views_14d);n2('g_vuniq',g.views_uniques_14d);
  n2('g_clones',g.clones_14d);n2('g_forks',g.forks);
 }catch(e){}
}
loadCommunity();setInterval(loadCommunity,300000);
</script>"""


def create_app() -> FastAPI:
    app = FastAPI(title="Feeless402", version=__version__)
    price_raw = xno_to_raw(PRICE_XNO)

    @app.get("/")
    def index():
        return {
            "service": "Feeless402 demo server",
            "paid_endpoints": {"/premium": f"{PRICE_XNO} XNO"},
            "free_endpoints": ["/faucet (POST {address})", "/llms.txt",
                               "/.well-known/railhint.json"],
            "treasury": server_wallet.address,
            "stats": f"{SITE_URL}/stats",
            "docs": DOCS_URL,
        }

    @app.get("/stats.json")
    def stats_json():
        return _stats_data()

    @app.get("/stats", response_class=HTMLResponse)
    def stats_page():
        return STATS_HTML

    @app.get("/.well-known/railhint.json")
    def wellknown():
        return rail_hint(price_raw)

    @app.get("/llms.txt", response_class=PlainTextResponse)
    def llms():
        return (
            "# Feeless402 — feeless x402 micropayments for AI agents\n\n"
            "This server sells API responses for XNO (Nano): zero network\n"
            "fees, sub-second settlement, prices with no minimum floor.\n\n"
            "## Pay-per-call flow\n"
            "1. GET /premium → HTTP 402 with a quote in `accepts` "
            "(scheme=exact, network=nano:mainnet)\n"
            "2. Sign a Nano send state block for `amount` raw to `payTo`\n"
            "3. Retry with the base64 payload in the PAYMENT-SIGNATURE "
            "(or X-PAYMENT) header\n\n"
            "## Zero-to-paying in three commands\n"
            "pip install feeless402\n"
            "nano-pay init\n"
            f"nano-pay claim {SITE_URL}   # free starter XNO from the faucet\n\n"
            "## Top up for real work\n"
            "nano-pay topup 5 --asset USDC-BASE --execute\n"
            "# $5 of USDC ≈ 100,000+ micro-calls at this endpoint's price\n\n"
            f"Docs: {DOCS_URL}\nSpec: x402 exact scheme, nano:mainnet\n"
            "x402 is stewarded by the Linux Foundation's x402 Foundation "
            "(premier members incl. Coinbase, Cloudflare, Stripe, Google, "
            "AWS, Circle, Visa, Mastercard)\n"
        )

    @app.get("/premium")
    def premium(request: Request):
        resource = str(request.url)
        try:
            block = _extract_block(request)
            if block is None:
                body = payment_required_body(
                    price_raw, server_wallet.address, resource
                )
                return JSONResponse(
                    body,
                    status_code=402,
                    headers={
                        "PAYMENT-REQUIRED": base64.b64encode(
                            json.dumps(body).encode()
                        ).decode()
                    },
                )
            if not _block_shape_ok(block):
                return JSONResponse(
                    {"error": "payment invalid: malformed block"},
                    status_code=402,
                )
            payer = verify_block(
                block, price_raw, server_wallet.address, rpc
            )
            receipt = settle_block(block, rpc)
            _count_paid_call(payer, demo_wallet.address)
        except PaymentInvalid as e:
            return JSONResponse(
                {"error": f"payment invalid: {e}"}, status_code=402
            )
        except (KeyError, ValueError, TypeError):
            return JSONResponse(
                {"error": "payment invalid: malformed block"},
                status_code=402,
            )
        return JSONResponse(
            {
                "premium": True,
                "message": (
                    "You just paid a fraction of a mill for this via a "
                    "feeless rail. Welcome to the machine economy."
                ),
                "payer": payer,
                "paid_xno": raw_to_xno(price_raw),
                "timestamp": int(time.time()),
            },
            headers={
                "PAYMENT-RESPONSE": base64.b64encode(
                    json.dumps(receipt).encode()
                ).decode()
            },
        )

    # ---------- live paywall demo (the site pays itself, publicly) ----------
    DEMO_ARTICLE = {
        "title": "The paywall you just beat cost $0.00004",
        "body": [
            "Here is what actually happened when you clicked. Your browser told "
            "this site's demo agent to go fetch the rest of this article. The "
            "server answered the way every paid API on the new machine web "
            "answers: HTTP 402 Payment Required, with a price attached — "
            "0.0001 XNO, the true metered cost of serving one article.",
            "The agent didn't sigh, open a subscription page, or type a card "
            "number. It signed a send block from its own self-custodied Nano "
            "wallet and retried the request. The server verified the payment "
            "on the public ledger, settled it, and released the text you are "
            "reading. Round trip: about a second. Network fees: zero.",
            "The receipt is not a metaphor. The block hash under this article "
            "is a real transaction on the Nano ledger — follow it to a public "
            "explorer and watch your click sit there, confirmed, forever.",
            "This price is impossible almost everywhere else. Card rails "
            "charge ~30¢ just to exist, so nobody meters articles. Gas-chain "
            "rails floor out around $0.001, so micro-content gets bundled "
            "into subscriptions you forget to cancel. A feeless rail has no "
            "floor — so a paywall can finally charge what a page is worth: "
            "almost nothing, an unlimited number of times.",
            "The standard is x402. The rail is Nano. The code is MIT. Your "
            "site could do this by lunchtime: pip install feeless402.",
        ],
    }

    from threading import Lock, Thread
    _demo_lock = Lock()
    _demo_hits = {"ips": {}, "window": []}

    def _demo_warm():  # boot-time: stock the wallet + precompute PoW
        with _demo_lock:
            try:
                demo_wallet.receive_all(rpc, prework=False)
                if demo_wallet.synced_account(rpc).raw_bal < 20 * price_raw:
                    with FAUCET_LOCK:
                        fh = faucet_wallet.send(
                            rpc, demo_wallet.address, 200 * price_raw,
                            prework=False)
                    threading.Thread(
                        target=lambda: faucet_wallet.prework(fh, rpc),
                        daemon=True).start()
                    demo_wallet.receive_all(rpc, prework=False)
                acct = demo_wallet.synced_account(rpc)
                demo_wallet.prework(demo_wallet._work_root(acct), rpc)
            except Exception:
                pass
    Thread(target=_demo_warm, daemon=True).start()

    @app.get("/demo/article")
    def demo_article(request: Request):
        """The article itself lives behind a genuine 402 — no theater."""
        resource = str(request.url)
        try:
            block = _extract_block(request)
            if block is None:
                body = payment_required_body(
                    price_raw, server_wallet.address, resource
                )
                return JSONResponse(
                    body, status_code=402,
                    headers={"PAYMENT-REQUIRED": base64.b64encode(
                        json.dumps(body).encode()).decode()},
                )
            if not _block_shape_ok(block):
                return JSONResponse(
                    {"error": "payment invalid: malformed block"},
                    status_code=402,
                )
            demo_payer = verify_block(
                block, price_raw, server_wallet.address, rpc)
            receipt = settle_block(block, rpc)
            _count_paid_call(demo_payer, demo_wallet.address)
        except PaymentInvalid as e:
            return JSONResponse(
                {"error": f"payment invalid: {e}"}, status_code=402
            )
        except (KeyError, ValueError, TypeError):
            return JSONResponse(
                {"error": "payment invalid: malformed block"},
                status_code=402,
            )
        return JSONResponse(
            {"article": DEMO_ARTICLE, "paid_xno": raw_to_xno(price_raw),
             "timestamp": int(time.time())},
            headers={"PAYMENT-RESPONSE": base64.b64encode(
                json.dumps(receipt).encode()).decode()},
        )

    @app.post("/demo/run")
    def demo_run(request: Request):
        """Drive the real client loop against /demo/article and narrate it."""
        ip = _client_ip(request)
        now = time.time()
        if now - _demo_hits["ips"].get(ip, 0) < 15:
            return JSONResponse(
                {"error": "one unlock per 15 seconds per visitor — easy"},
                status_code=429)
        _demo_hits["window"] = [t for t in _demo_hits["window"] if now - t < 60]
        _demo_hits["ips"] = {k: v for k, v in _demo_hits["ips"].items()
                             if now - v < 60}
        if len(_demo_hits["window"]) >= 12:
            return JSONResponse(
                {"error": "demo is busy — try again in a minute"},
                status_code=429)
        _demo_hits["ips"][ip] = now
        _demo_hits["window"].append(now)

        import threading

        import requests as _rq

        from .x402 import (build_payment_header, offer_amount_raw,
                           offer_pay_to, parse_quote, pick_nano_offer)
        target = f"{SITE_URL}/demo/article"
        with _demo_lock:
            t0 = time.time()
            try:
                r1 = _rq.get(target, timeout=30)
                if r1.status_code != 402:
                    raise RuntimeError(f"expected 402, got {r1.status_code}")
                t_quote = time.time()
                quote = parse_quote(r1)
                offer = pick_nano_offer(quote)
                amount = offer_amount_raw(offer)
                if amount > price_raw * 2:
                    raise RuntimeError("quote exceeds demo cap")
                block, new_frontier, work_root = (
                    demo_wallet.build_payment_block(
                        rpc, offer_pay_to(offer), amount))
                hdr = build_payment_header(
                    quote, offer, block, offer_pay_to(offer))
                t_sign = time.time()
                r2 = _rq.get(target, timeout=60, headers={
                    "PAYMENT-SIGNATURE": hdr, "X-PAYMENT": hdr})
                t_settle = time.time()
                print(f"[demo] quote={t_quote-t0:.2f}s "
                      f"sign={t_sign-t_quote:.2f}s "
                      f"settle={t_settle-t_sign:.2f}s", flush=True)
            except Exception as e:
                return JSONResponse(
                    {"error": f"demo hiccup: {e}"}, status_code=500)
            elapsed = round(time.time() - t0, 2)  # click → paid response
        receipt = {}
        rec_hdr = r2.headers.get("payment-response")
        if rec_hdr:
            try:
                receipt = json.loads(base64.b64decode(rec_hdr))
            except Exception:
                pass
        ok = 200 <= r2.status_code < 300

        def _demo_finish():  # next-run PoW + wallet upkeep, off the clock
            with _demo_lock:
                try:
                    if ok:
                        demo_wallet.payment_succeeded(
                            rpc, new_frontier, work_root)
                    else:
                        demo_wallet.payment_failed(work_root)
                    demo_wallet.receive_all(rpc, prework=False)
                    if demo_wallet.synced_account(
                            rpc).raw_bal < 20 * price_raw:
                        with FAUCET_LOCK:
                            fh = faucet_wallet.send(
                                rpc, demo_wallet.address, 200 * price_raw,
                                prework=False)
                        threading.Thread(
                            target=lambda: faucet_wallet.prework(fh, rpc),
                            daemon=True).start()
                        demo_wallet.receive_all(rpc, prework=False)
                    acct = demo_wallet.synced_account(rpc)
                    demo_wallet.prework(demo_wallet._work_root(acct), rpc)
                except Exception:
                    pass
        threading.Thread(target=_demo_finish, daemon=True).start()

        if not ok or not receipt.get("hash"):
            return JSONResponse(
                {"error": f"payment did not settle (HTTP {r2.status_code})"},
                status_code=500)
        offer = (quote.get("accepts") or [{}])[0]
        ledger = None
        try:  # the on-page "view from the blockchain": block_info from the node
            info = rpc.call({"action": "block_info", "json_block": "true",
                             "hash": receipt["hash"]})
            contents = info.get("contents") or {}
            ledger = {
                "hash": receipt["hash"],
                "confirmed": str(info.get("confirmed")).lower() == "true",
                "amount_xno": str(raw_to_xno(int(info.get("amount", 0)))),
                "from": contents.get("account", demo_wallet.address),
                "to": server_wallet.address,
                "type": contents.get("subtype") or "send",
                "timestamp": int(info.get("local_timestamp") or time.time()),
            }
        except Exception:
            pass
        return {
            "amount_xno": str(raw_to_xno(int(offer.get("amount") or price_raw))),
            "payer": demo_wallet.address,
            "hash": receipt["hash"],
            "confirmed": bool(receipt.get("confirmed")),
            "explorer": f"https://nanexplorer.com/nano/block/{receipt['hash']}",
            "elapsed_s": elapsed,
            "article": r2.json().get("article"),
            "ledger": ledger,
        }

    _compare_cache = {"ts": 0.0, "data": None}

    @app.get("/demo/compare")
    def demo_compare():
        """Live rail comparison from a real multi-rail merchant (nano-gpt).

        Dry-run quote only — no payment is made. Cached 10 min to stay
        polite to their endpoint."""
        now = time.time()
        if _compare_cache["data"] and now - _compare_cache["ts"] < 600:
            return {**_compare_cache["data"], "cached": True}
        import requests as _rq

        from .x402 import compare_rails, parse_quote
        try:
            r = _rq.post(
                "https://nano-gpt.com/api/v1/chat/completions",
                headers={"x-x402": "true",
                         "Content-Type": "application/json"},
                json={"model": "gpt-5-nano",
                      "messages": [{"role": "user", "content": "hi"}],
                      "max_tokens": 1},
                timeout=30)
            if r.status_code != 402:
                raise RuntimeError(f"expected 402, got {r.status_code}")
            rows, seen = [], set()
            for row in compare_rails(parse_quote(r)):
                key = (row.get("payer_sends"),
                       (row.get("network") or "").replace("-", ":"))
                if key in seen:
                    continue
                seen.add(key)
                svc = row.get("service_cost_usd") or 0
                paid = row.get("payer_cost_usd") or 0
                rows.append({
                    "network": row.get("network"),
                    "asset": row.get("asset"),
                    "payer_sends": row.get("payer_sends"),
                    "usd": paid,
                    "markup": round(paid / svc, 1) if svc else None,
                    "feeless": bool(row.get("feeless")),
                })
            data = {
                "source": "nano-gpt.com — one-token chat completion "
                          "(smallest meterable call)",
                "rows": rows,
                "fetched_unix": now,
            }
            _compare_cache.update(ts=now, data=data)
            return {**data, "cached": False}
        except Exception as e:
            if _compare_cache["data"]:
                return {**_compare_cache["data"], "cached": True,
                        "stale": True}
            return JSONResponse(
                {"error": f"live quote unavailable: {e}"}, status_code=503)

    _gas_cache = {"ts": 0.0, "data": None}

    @app.get("/demo/gas")
    def demo_gas():
        """Live Base gas → the real cost behind one USDC settlement.

        Shows why the $0.001 price floor exists at current prices.
        Cached 10 min."""
        now = time.time()
        if _gas_cache["data"] and now - _gas_cache["ts"] < 600:
            return {**_gas_cache["data"], "cached": True}
        import requests as _rq
        try:
            g = _rq.post(
                "https://mainnet.base.org",
                json={"jsonrpc": "2.0", "method": "eth_gasPrice",
                      "params": [], "id": 1},
                timeout=10).json()
            gas_wei = int(g["result"], 16)
            eth = _rq.get(
                "https://api.coingecko.com/api/v3/simple/price"
                "?ids=ethereum&vs_currencies=usd",
                timeout=10).json()["ethereum"]["usd"]
            gas_units = 80000  # ≈ ERC-20 transferWithAuthorization
            fee = gas_wei * gas_units / 1e18 * eth
            data = {"base_gas_gwei": round(gas_wei / 1e9, 4),
                    "eth_usd": eth, "gas_units_est": gas_units,
                    "usdc_settlement_fee_usd": fee,
                    "fetched_unix": now}
            _gas_cache.update(ts=now, data=data)
            return {**data, "cached": False}
        except Exception as e:
            if _gas_cache["data"]:
                return {**_gas_cache["data"], "cached": True, "stale": True}
            return JSONResponse(
                {"error": f"gas data unavailable: {e}"}, status_code=503)

    @app.get("/faucet/challenge")
    def faucet_challenge(address: str = ""):
        """Sybil resistance: claims must include client-computed PoW over
        blake2b(address) at send difficulty. A legit agent pays ~30s of
        CPU once; a sybil farm burns CPU linearly per fake address —
        for a grant worth a fraction of a cent."""
        import hashlib

        if not FAUCET_POW:
            return {"pow_required": False}
        root = hashlib.blake2b(address.encode(), digest_size=32).hexdigest()
        return {
            "pow_required": True,
            "root": root,
            "difficulty": "fffffff800000000",
            "how": "work_generate(root, difficulty) locally; POST it as 'work'",
        }

    @app.get("/faucet")
    def faucet_info():
        """A human clicking a pasted /faucet link gets a signpost, not a 405."""
        return {
            "how": "POST JSON {\"address\": \"nano_...\", \"work\": \"...\"} "
                   "— GET /faucet/challenge first for the PoW root",
            "humans": f"{SITE_URL}/faucet.html",
            "xno_per_claim": FAUCET_XNO,
        }

    @app.post("/faucet")
    async def faucet(request: Request):
        try:
            body = await request.json()
            address = str(body.get("address", ""))
        except Exception:
            return JSONResponse({"error": "POST JSON {\"address\": \"nano_...\"}"},
                                status_code=400)
        if not address.startswith(("nano_", "xrb_")) or len(address) < 60:
            return JSONResponse({"error": "invalid nano address"}, status_code=400)

        if FAUCET_POW:
            import hashlib

            import nanopy as _np

            root = hashlib.blake2b(address.encode(), digest_size=32).hexdigest()
            work = str(body.get("work", ""))
            try:
                ok = bool(
                    _np.ext.work_validate(
                        int(work, 16),
                        bytes.fromhex(root),
                        int("fffffff800000000", 16),
                    )
                )
            except Exception:
                ok = False
            if not ok:
                return JSONResponse(
                    {"error": "PoW required — GET /faucet/challenge first"},
                    status_code=402,
                )

        led = _ledger()
        if address in led["addresses"]:
            return JSONResponse(
                {"error": "address already claimed its starter XNO",
                 "note": "one claim per address, ever — generate a new "
                         "address to claim again",
                 "next": f"holding XNO already? retry {SITE_URL}/premium"},
                status_code=429,
            )
        ip = _client_ip(request)
        day_ago = time.time() - 86400
        recent = [t for t in led["ips"].get(ip, []) if t > day_ago]
        if len(recent) >= FAUCET_PER_IP_PER_DAY:
            # Say what the limit is, when it lifts, and what to do meanwhile —
            # an integrator testing in a loop deserves better than "no".
            frees_at = min(recent) + 86400
            return JSONResponse(
                {"error": "daily faucet allotment used up for this IP",
                 "limit_per_ip_per_day": FAUCET_PER_IP_PER_DAY,
                 "claims_used_last_24h": len(recent),
                 "retry_after_seconds": max(1, int(frees_at - time.time())),
                 "retry_at_utc": time.strftime(
                     "%Y-%m-%dT%H:%M:%SZ", time.gmtime(frees_at)),
                 "note": "rolling 24h window per IP; separately, each address "
                         "may claim once ever",
                 "next": "already holding starter XNO? just retry the paid "
                         f"endpoint: {SITE_URL}/premium",
                 "building_something": "if you need a bigger allowance for an "
                                       f"integration, ask: {DOCS_URL}/issues"},
                status_code=429,
                headers={"Retry-After": str(max(1, int(frees_at - time.time())))},
            )

        amount = xno_to_raw(FAUCET_XNO)

        def _locked_claim():
            # Blocking RPC work runs in a worker thread (not on the event
            # loop) under FAUCET_LOCK so faucet sends can't fork against
            # the demo-refill threads; ledger is re-checked inside the
            # lock so concurrent same-address claims can't double-spend.
            with FAUCET_LOCK:
                led2 = _ledger()
                if address in led2["addresses"]:
                    return None
                faucet_wallet.load()
                # pocket any pending refills first
                faucet_wallet.receive_all(rpc, prework=False)
                # prework=False: computing the NEXT block's PoW takes minutes
                # on this VPS. Doing it here would hold the lock and the
                # client's connection long after their XNO was already sent.
                h = faucet_wallet.send(rpc, address, amount, prework=False)
                led2["addresses"][address] = time.time()
                led2["ips"].setdefault(ip, []).append(time.time())
                _save_ledger(led2)
                return h

        try:
            h = await anyio.to_thread.run_sync(_locked_claim)
        except Exception as e:
            return JSONResponse(
                {"error": f"faucet dry or failed: {e}",
                 "refill_address": faucet_wallet.address},
                status_code=503,
            )
        if h is None:
            return JSONResponse(
                {"error": "address already claimed its starter XNO"},
                status_code=429,
            )
        # Warm the next claim's PoW off the request path. No lock needed:
        # prework only computes and caches work, it never signs or
        # broadcasts a block.
        threading.Thread(
            target=lambda: faucet_wallet.prework(h, rpc), daemon=True
        ).start()
        return {
            "sent_xno": FAUCET_XNO,
            "to": address,
            "hash": h,
            "next": "run `nano-pay receive`, then retry the paid endpoint",
        }

    # Answer HEAD wherever GET is served — probes and link checkers use it
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if methods and "GET" in methods:
            methods.add("HEAD")

    return app


def serve(port: int = 8402, host: str = "127.0.0.1"):
    import uvicorn

    print(f"Feeless402 server on {host}:{port}")
    print(f"  treasury: {server_wallet.address}")
    print(f"  faucet:   {faucet_wallet.address} (fund this to enable claims)")
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
