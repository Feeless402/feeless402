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
LOGS = ["/var/log/nginx/access.log", "/var/log/nginx/access.log.1"]
KEEP_DAYS = 21
SHOW_DAYS = 14

BOT_PATH = re.compile(r"\.php|/wp-|xmlrpc|\.env|\.git|/vendor/|/media/|\.asp|\.cgi")
BOT_UA = re.compile(r"bot|crawl|spider|slurp|scan|censys|shodan", re.I)
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
    days = {d: set(ips) for d, ips in state.items()}
    for path in LOGS:
        if not os.path.exists(path):
            continue
        with open(path, errors="replace") as f:
            for line in f:
                m = LINE.match(line)
                if not m:
                    continue
                ip, dd, mon, yyyy, url, ua = m.groups()
                if BOT_PATH.search(url) or BOT_UA.search(ua):
                    continue
                date = f"{yyyy}-{MON[mon]:02d}-{dd}"
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
