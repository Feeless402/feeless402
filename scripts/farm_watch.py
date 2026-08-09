#!/usr/bin/env python3
"""Did the farm keep paying after the top-up was switched off?

Aug 7 2026: the graduation top-up (0.0045) was set to zero. The farm had been
paying our 0.0001 /premium fee purely to unlock it — a 45x return. If their
script hasn't noticed, they now pay us 0.0001 per address and receive only the
0.0005 grant, which is a much duller trade for them and a small joke for us.

Prints the post-cutover ledger: grants out, farm payments in, and the net.
Farm signature = the control node's `Bun/` user-agent on /premium (see the
five signatures in the ops notes); everything else is treated as a real user
and reported separately, because a real payer is the good news, not the joke.

Usage: farm_watch.py [--since YYYY-MM-DDTHH:MM:SSZ]   (default = cutover)
"""

import glob
import gzip
import re
import sys
from datetime import datetime, timezone

CUTOVER = "2026-08-07T14:52:00Z"
LOGS = "/var/log/nginx/feeless402.access.log*"
GRANT_XNO = 0.0005
PRICE_XNO = 0.0001

LINE = re.compile(
    r'^(\S+) \S+ \S+ \[(\d{2})/(\w{3})/(\d{4}):(\d{2}:\d{2}:\d{2}) [^\]]*\] '
    r'"(\S+) (\S+) [^"]*" (\d{3}) \S+ "[^"]*" "([^"]*)"'
)
MONTHS = {m: i for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


def parse(since_s):
    since = datetime.strptime(since_s, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)
    grants, farm_pay, real_pay = [], [], []
    for path in sorted(glob.glob(LOGS)):
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", errors="replace") as f:
            for ln in f:
                m = LINE.match(ln)
                if not m:
                    continue
                ip, d, mon, yr, hms, method, url, status, ua = m.groups()
                ts = datetime.strptime(
                    f"{yr}-{MONTHS[mon]:02d}-{d} {hms}", "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
                if ts < since or status != "200":
                    continue
                base = url.split("?")[0]
                if method == "POST" and base == "/faucet":
                    grants.append((ts, ip))
                elif base == "/premium":
                    (farm_pay if ua.startswith("Bun/") else real_pay).append((ts, ip))
    return grants, farm_pay, real_pay


def main():
    since = CUTOVER
    if "--since" in sys.argv:
        since = sys.argv[sys.argv.index("--since") + 1]
    grants, farm_pay, real_pay = parse(since)
    out = len(grants) * GRANT_XNO
    farm_in = len(farm_pay) * PRICE_XNO
    real_in = len(real_pay) * PRICE_XNO

    print(f"since {since}")
    print(f"  faucet grants out : {len(grants):4d}  = {out:.4f} XNO")
    print(f"  farm payments in  : {len(farm_pay):4d}  = {farm_in:.4f} XNO"
          f"   <- the chuckle counter")
    print(f"  real payments in  : {len(real_pay):4d}  = {real_in:.4f} XNO")
    print(f"  net               : {real_in + farm_in - out:+.4f} XNO")
    if farm_pay:
        print(f"\n  🤣 the farm is still paying: {len(farm_pay)} calls "
              f"(0.0001 each) for a 0.0005 grant — they now hand back 20% of "
              f"every grant they take.")
        print(f"     first post-cutover payment: "
              f"{farm_pay[0][0].strftime('%b %-d %H:%M:%SZ')} from {farm_pay[0][1]}")
        print(f"     latest:                     "
              f"{farm_pay[-1][0].strftime('%b %-d %H:%M:%SZ')} from {farm_pay[-1][1]}")
    else:
        print("\n  no farm payments since the change — either they noticed "
              "fast, or they haven't run since.")
    if real_pay:
        print(f"\n  ⭐ {len(real_pay)} REAL paid call(s) in this window "
              f"(non-Bun UA) — check these by hand, they matter more.")


if __name__ == "__main__":
    main()
