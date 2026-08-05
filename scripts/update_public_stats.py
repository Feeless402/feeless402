#!/usr/bin/env python3
"""Build /var/www/feeless402/community-stats.json — public counters for the stats page.

Sources (all fail-soft: on error, keep the previous run's values):
  - pypistats.org  -> daily pip installs, mirrors excluded
  - gh api         -> repo stars/forks + 14d traffic (views/clones; owner-auth'd)
  - nginx logs     -> unique site visitors per day (bot noise filtered)

Run from cron a few times a day. Publishes counts only — never IPs.
"""
import json
import os
import re
import subprocess
import time
import urllib.request

REPO = "Feeless402/feeless402"
OUT = "/var/www/feeless402/community-stats.json"
STATE = "/root/nano-pay/logs/visitor_state.json"
# Per-vhost logs since Aug 5 2026 (server-level access_log overrides the
# global one, so the shared access.log no longer sees feeless402 traffic).
# The shared logs stay listed until their pre-split history rotates out.
LOGS = ["/var/log/nginx/feeless402.access.log",
        "/var/log/nginx/feeless402.access.log.1"]
# Pre-split history: the shared log held EVERY vhost (and the catch-all
# default server), so counting its IPs unfiltered inflated visitors ~7x.
# Only requests to feeless402-specific paths count from these.
SHARED_LOGS = ["/var/log/nginx/access.log", "/var/log/nginx/access.log.1"]
# Human-facing surface only. Agent endpoints (/llms.txt, /premium, /faucet,
# /mcp, /.well-known/*) are deliberately excluded: bare /llms.txt probes hit
# every domain on the box, and agent traffic is counted elsewhere as claims
# and paid calls, not as site visitors. /demo/gas fires on homepage load,
# which is how a plain "/" visit is attributed to feeless402 in shared logs.
F402_PATH = re.compile(
    r"^/(?:demo/(?:gas|compare|run|article)|faucet\.html|docs\.html"
    r"|stats|stats\.json|community-stats\.json|eli5\.png)(?:[/?]|$)")
# Visitor attribution only became reliable on Aug 4, when the homepage
# started fetching /demo/gas on load. Before that, feeless402 visitors who
# stayed on "/" are indistinguishable from other vhosts in the shared log,
# so those days would publish a floor (1-3) that reads as "nobody came"
# when reddit was actually driving traffic. Better no bar than a wrong one.
LAUNCH_DAY = "2026-08-04"
KEEP_DAYS = 21
SHOW_DAYS = 14

BOT_PATH = re.compile(r"\.php|/wp-|xmlrpc|\.env|\.git|/vendor/|/media/|\.asp|\.cgi")
BOT_UA = re.compile(
    r"bot|crawl|spider|slurp|scan|censys|shodan|masscan|zgrab|nuclei|sqlmap"
    r"|nmap|go-http-client|^curl|^wget|wp-admin|catalog|checker|evidence"
    r"|arrivals|monitor|probe|fetch|http-client|python-requests|okhttp"
    r"|headless|java/|libwww|axios|node-fetch", re.I)
# Allowlist beats denylist: a visitor must look like a real browser, and
# unmaintained-version UAs are the tell for cheap spoofers.
BROWSERISH = re.compile(r"Mozilla/5\.0.*(Chrome|Firefox|Safari|Edg)/", re.I)
_CHROME_V = re.compile(r"Chrome/(\d+)")
_FF_V = re.compile(r"Firefox/(\d+)")
_IOS_V = re.compile(r"(?:iPhone|CPU) OS (\d+)")


def is_human_ua(ua):
    """Heuristic: browser-shaped UA at a plausibly current version."""
    if BOT_UA.search(ua) or not BROWSERISH.search(ua):
        return False
    m = _CHROME_V.search(ua)
    if m and int(m.group(1)) < 110:   # Chrome 110 = early 2023
        return False
    m = _FF_V.search(ua)
    if m and int(m.group(1)) < 110:
        return False
    m = _IOS_V.search(ua)
    if m and int(m.group(1)) < 13:
        return False
    return True


def _self_ips():
    """Operator self-traffic (server, home) — one IP per line, outside repo."""
    try:
        with open(os.path.expanduser("~/.nano-pay/self-ips.txt")) as f:
            return {l.strip() for l in f
                    if l.strip() and not l.startswith("#")}
    except OSError:
        return set()


SELF_IPS = _self_ips() | {"127.0.0.1", "::1"}
LINE = re.compile(r'^(\S+) \S+ \S+ \[(\d{2})/([A-Za-z]{3})/(\d{4}):[^\]]*\] "(?:GET|POST|HEAD) ([^ "]+)[^"]*" \d{3} \S+ "[^"]*" "([^"]*)"')
MON = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def http_json(url, retries=1):
    req = urllib.request.Request(url, headers={"User-Agent": "feeless402-stats"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if attempt < retries and e.code in (429, 502, 503):
                time.sleep(2)
                continue
            raise


def gh_json(path):
    out = subprocess.run(["gh", "api", path], capture_output=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.decode()[:200])
    return json.loads(out.stdout)


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def pypi_daily():
    data = http_json("https://pypistats.org/api/packages/feeless402/overall")["data"]
    days = {}
    for row in data:
        if row["category"] == "without_mirrors":
            days[row["date"]] = row["downloads"]
    return [{"date": d, "installs": days[d]} for d in sorted(days)[-SHOW_DAYS:]]


def github_stats():
    repo = gh_json(f"repos/{REPO}")
    views = gh_json(f"repos/{REPO}/traffic/views")
    clones = gh_json(f"repos/{REPO}/traffic/clones")
    return {
        "stars": repo.get("stargazers_count"),
        "forks": repo.get("forks_count"),
        "views_14d": views.get("count"),
        "views_uniques_14d": views.get("uniques"),
        "clones_14d": clones.get("count"),
        "clones_uniques_14d": clones.get("uniques"),
    }


def visitors():
    state = load_json(STATE)  # {date: [ip, ...]}
    days = {d: {ip for ip in ips if ip not in SELF_IPS}
            for d, ips in state.items()}
    for path in LOGS + SHARED_LOGS:
        if not os.path.exists(path):
            continue
        shared = path in SHARED_LOGS
        with open(path, errors="replace") as f:
            for line in f:
                m = LINE.match(line)
                if not m:
                    continue
                ip, dd, mon, yyyy, url, ua = m.groups()
                if ip in SELF_IPS or BOT_PATH.search(url) or not is_human_ua(ua):
                    continue
                if shared and not F402_PATH.match(url):
                    continue  # another vhost's traffic in the pre-split log
                date = f"{yyyy}-{MON[mon]:02d}-{dd}"
                if date < LAUNCH_DAY:
                    continue
                days.setdefault(date, set()).add(ip)
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - KEEP_DAYS * 86400))
    days = {d: ips for d, ips in days.items() if d >= cutoff}
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({d: sorted(ips) for d, ips in days.items()}, f)
    os.replace(tmp, STATE)
    return [{"date": d, "uniques": len(days[d])} for d in sorted(days)[-SHOW_DAYS:]]


def main():
    prev = load_json(OUT)
    out = {"generated_unix": time.time()}
    for key, fn in (("pypi_daily", pypi_daily), ("github", github_stats),
                    ("visitors", visitors)):
        try:
            out[key] = fn()
        except Exception as e:
            out[key] = prev.get(key)
            print(f"[warn] {key}: {e}")
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f)
    os.replace(tmp, OUT)
    os.chmod(OUT, 0o644)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
