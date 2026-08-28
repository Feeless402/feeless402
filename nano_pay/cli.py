"""nano-pay CLI — agent-friendly: everything prints compact JSON."""

import argparse
import json
import sys
import time

from . import raw_to_xno, xno_to_raw
from .rpc import RPC
from .topup import TopupError, create_order, estimate, limits
from .wallet import Wallet, WalletError
from .x402 import PriceCapExceeded, X402Error, request_with_payment

DEFAULT_MAX_PAY_XNO = "0.05"  # per-payment safety cap unless overridden


def out(obj, code=0):
    print(json.dumps(obj, indent=2, default=str))
    sys.exit(code)


def cmd_init(args):
    w = Wallet()
    if w.exists() and not args.seed:
        w.load()
        out({"status": "exists", "address": w.address, "file": str(w.path)})
    w.create(seed=args.seed)
    out(
        {
            "status": "created",
            "address": w.address,
            "file": str(w.path),
            "note": "fund this address with XNO (see `nano-pay topup`), "
            "then run `nano-pay receive`",
        }
    )


def cmd_address(args):
    out({"address": Wallet().load().address})


def cmd_status(args):
    w = Wallet().load()
    rpc = RPC()
    acct = w.synced_account(rpc)
    pending = rpc.receivable(w.address)
    out(
        {
            "address": w.address,
            "balance_xno": raw_to_xno(acct.raw_bal),
            "balance_raw": str(acct.raw_bal),
            "receivable_count": len(pending),
            "receivable_xno": raw_to_xno(sum(pending.values())),
            "opened": acct.frontier != "0" * 64,
        }
    )


def cmd_receive(args):
    w = Wallet().load()
    rpc = RPC()
    got = w.receive_all(rpc, prework=True)
    acct = w.synced_account(rpc)
    out(
        {
            "received": [
                {"hash": h, "amount_xno": raw_to_xno(a)} for h, a in got
            ],
            "balance_xno": raw_to_xno(acct.raw_bal),
        }
    )


def cmd_send(args):
    w = Wallet().load()
    rpc = RPC()
    h = w.send(rpc, args.to, xno_to_raw(args.amount))
    # Receipt first, then warm the next block's PoW (minutes, and nobody is
    # waiting on it). out() exits the process, so print and flush before.
    print(json.dumps({"status": "sent", "hash": h, "to": args.to,
                      "amount_xno": args.amount}, indent=2, default=str),
          flush=True)
    print("sent. now pre-computing the next block's proof-of-work so your "
          "next send is instant — safe to Ctrl-C.", file=sys.stderr, flush=True)
    try:
        w.prework(h, rpc)
    except Exception:
        pass
    sys.exit(0)


def cmd_prework(args):
    w = Wallet().load()
    rpc = RPC()
    acct = w.synced_account(rpc)
    root = acct.pk if acct.frontier == "0" * 64 else acct.frontier
    w.prework(root, rpc)
    out({"status": "work cached", "root": root})


def _do_request(args, dry_run):
    w = Wallet().load() if not dry_run else (Wallet().load() if Wallet().exists() else None)
    rpc = RPC()
    kwargs = {}
    if args.json:
        kwargs["json"] = json.loads(args.json)
    headers = {}
    for hdr in args.header or []:
        k, _, v = hdr.partition(":")
        headers[k.strip()] = v.strip()
    method = args.method or ("POST" if args.json else "GET")
    r, info = request_with_payment(
        method,
        args.url,
        w,
        rpc,
        max_raw=xno_to_raw(args.max_xno),
        headers=headers,
        dry_run=dry_run,
        **kwargs,
    )
    body = r.text
    try:
        body = r.json()
    except Exception:
        pass
    result = (
        {
            "status_code": r.status_code,
            "paid": (not dry_run) and info is not None,
            "payment": info,
            "body": body if not args.body_only else None,
        }
        if not args.body_only
        else body
    )
    if dry_run or info is None:
        out(result)
    # Paid: give the caller their response first, then warm the next block's
    # PoW so the following payment is instant. out() exits, so print first.
    print(json.dumps(result, indent=2, default=str), flush=True)
    print("payment settled. now pre-computing the next block's proof-of-work "
          "so your next payment is instant — safe to Ctrl-C.",
          file=sys.stderr, flush=True)
    try:
        w.prework(w._work_root(w.synced_account(rpc)), rpc)
    except Exception:
        pass
    sys.exit(0)


def cmd_quote(args):
    args.max_xno = DEFAULT_MAX_PAY_XNO
    args.body_only = False
    _do_request(args, dry_run=True)


def cmd_pay(args):
    _do_request(args, dry_run=False)


def cmd_topup(args):
    w = Wallet().load()
    try:
        if args.execute:
            order = create_order(args.asset, args.amount, w.address)
            out({"status": "order created", "order": order})
        est = estimate(args.asset, args.amount)
        lim = limits(args.asset)
        out(
            {
                "from": f"{args.amount} {args.asset.upper()}",
                "estimated_xno": est,
                "limits": lim,
                "receive_address": w.address,
                "note": "re-run with --execute (needs NANSWAP_API_KEY) to "
                "create the order, then send the deposit",
            }
        )
    except TopupError as e:
        out({"error": str(e)}, code=1)


def cmd_compare(args):
    import requests as rq

    from .x402 import compare_rails, parse_quote

    headers = {"x-x402": "true"}
    for hdr in args.header or []:
        k, _, v = hdr.partition(":")
        headers[k.strip()] = v.strip()
    kwargs = {"json": json.loads(args.json)} if args.json else {}
    method = args.method or ("POST" if args.json else "GET")
    r = rq.request(method, args.url, headers=headers, timeout=60, **kwargs)
    if r.status_code != 402:
        out({"note": f"endpoint returned {r.status_code}, not 402 — nothing to compare"})
    rails = compare_rails(parse_quote(r))
    out({
        "endpoint": args.url,
        "rails": rails,
        "note": "prices quoted live by the server just now — verify, don't trust",
    })


def cmd_serve(args):
    from .server import serve

    serve(port=args.port, host=args.host)


def _claim_one(base, w):
    """Claim from one faucet, solving its PoW challenge if it has one."""
    import requests as rq

    # Accept the site root, the /faucet endpoint itself, or either with a
    # query string (e.g. ``https://feeless402.com/faucet?ref=skill`` as the
    # skill docs show) — normalize to the root and carry the query along so
    # attribution tags survive.
    from urllib.parse import parse_qsl, urlsplit

    u = urlsplit(base)
    path = u.path.rstrip("/")
    if path.endswith("/faucet"):
        path = path[: -len("/faucet")]
    base = f"{u.scheme}://{u.netloc}{path}"
    extra = dict(parse_qsl(u.query))
    extra.pop("address", None)
    payload = {"address": w.address}
    try:
        ch = rq.get(
            f"{base}/faucet/challenge",
            params={**extra, "address": w.address},
            timeout=30,
        ).json()
        if ch.get("pow_required"):
            import nanopy

            # Deliberately local: the faucet's challenge is meant to cost the
            # claimant real CPU, which is what stops a farm claiming in bulk.
            # It cannot be handed to a work service without giving that away.
            # It can, however, say so — running silent for minutes on the
            # first command anyone tries reads as a hang.
            print("solving the faucet's proof-of-work on this machine — "
                  "usually 30s to a few minutes. This is what keeps the "
                  "faucet from being drained; it only happens once.",
                  file=sys.stderr, flush=True)
            t0 = time.time()
            payload["work"] = (
                f"{nanopy.ext.work_generate(bytes.fromhex(ch['root']), int(ch['difficulty'], 16), __import__('os').urandom(128)):016x}"
            )
            print(f"proof-of-work solved in {time.time() - t0:.0f}s — claiming…",
                  file=sys.stderr, flush=True)
    except Exception:
        pass  # no challenge endpoint — plain claim
    r = rq.post(f"{base}/faucet", params=extra or None, json=payload, timeout=240)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:300]}


def cmd_claim(args):
    w = Wallet().load()
    faucets = [args.url] if args.url else []
    if args.auto:
        import requests as rq

        for registry in (args.url, "https://feeless402.com"):
            if not registry:
                continue
            try:
                hint = rq.get(
                    f"{registry.rstrip('/')}/.well-known/railhint.json",
                    timeout=20,
                ).json()
                faucets += [u for u in hint.get("faucets", []) if u not in faucets]
            except Exception:
                pass
    attempts = []
    for base in faucets:
        code, res = _claim_one(base, w)
        attempts.append({"faucet": base, "status": code, "result": res})
        if code == 200:
            rpc = RPC()
            # The faucet's send needs a moment to reach the node we're asking.
            # Poll rather than assume: a fast faucet reply used to be slow
            # enough to hide this, and a claim that reports 0 received looks
            # to the user like the faucet failed.
            got = []
            for attempt in range(20):
                got = w.receive_all(rpc, prework=False)
                if got:
                    break
                time.sleep(1.5)
            acct = w.synced_account(rpc)
            payload = {
                "claimed_from": base,
                "result": res,
                "received": [
                    {"hash": h, "amount_xno": raw_to_xno(a)}
                    for h, a in got
                ],
                "balance_xno": raw_to_xno(acct.raw_bal),
                "attempts": attempts,
            }
            # Report the grant BEFORE warming the next block. Pre-computing
            # takes minutes on a modest machine, and doing it first left the
            # user staring at a blank terminal wondering whether the claim
            # worked — on the very first command they ever run. Same order
            # and same guard as send()/pay().
            if got:
                print(json.dumps(payload, indent=2, default=str), flush=True)
                print("claimed — that was the slow part, and it is over. "
                      "Pre-computing the proof-of-work for your next block "
                      "now, and again after every transaction, so payments "
                      "from here are near-instant. Safe to Ctrl-C.",
                      file=sys.stderr, flush=True)
                try:
                    w.prework(got[-1][0], rpc)
                except Exception:
                    pass
                sys.exit(0)
            out(payload)
    # An already-claimed address is not a broken faucet: the money was
    # granted earlier (possibly to a client that gave up before the reply).
    # Pocket anything still pending and report the balance instead of a
    # hard error — exit 0 when the wallet is funded, since the agent can
    # proceed straight to paying.
    already = [
        a for a in attempts
        if a["status"] == 429
        and "already claimed" in str(a["result"].get("error", ""))
    ]
    if already:
        rpc = RPC()
        got = w.receive_all(rpc, prework=False)
        acct = w.synced_account(rpc)
        bal = raw_to_xno(acct.raw_bal)
        out(
            {
                "status": "already_claimed",
                "message": f"this address already received its starter XNO — "
                           f"balance {bal} XNO, nothing more to claim here; "
                           f"generate a fresh wallet if you need a new grant",
                "balance_xno": bal,
                "received_now": [
                    {"hash": h, "amount_xno": raw_to_xno(a)} for h, a in got
                ],
                "attempts": attempts,
            },
            code=0 if acct.raw_bal else 1,
        )
    out({"error": "no faucet granted a claim", "attempts": attempts}, code=1)


def main():
    p = argparse.ArgumentParser(
        prog="nano-pay",
        description="Self-custodied Nano wallet + x402 payment client for agents",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create wallet")
    s.add_argument("--seed", help="import existing 64-hex seed")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("address", help="show wallet address")
    s.set_defaults(fn=cmd_address)

    s = sub.add_parser("status", help="balance + receivable")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("receive", help="pocket all incoming XNO")
    s.set_defaults(fn=cmd_receive)

    s = sub.add_parser("send", help="send XNO directly")
    s.add_argument("to")
    s.add_argument("amount", help="amount in XNO")
    s.set_defaults(fn=cmd_send)

    s = sub.add_parser("prework", help="pre-compute PoW for next block")
    s.set_defaults(fn=cmd_prework)

    for name, fn, hlp in (
        ("quote", cmd_quote, "show an endpoint's x402 quote WITHOUT paying"),
        ("pay", cmd_pay, "request URL, auto-paying its Nano x402 quote"),
        ("compare", cmd_compare, "price the same call on every rail the server offers"),
    ):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("url")
        s.add_argument("--method")
        s.add_argument("--json", help="JSON request body")
        s.add_argument("--header", action="append", help="extra 'K: V' header")
        s.add_argument("--body-only", action="store_true")
        if name == "pay":
            s.add_argument(
                "--max-xno",
                default=DEFAULT_MAX_PAY_XNO,
                help=f"refuse quotes above this (default {DEFAULT_MAX_PAY_XNO})",
            )
        s.set_defaults(fn=fn)

    s = sub.add_parser("topup", help="quote/execute small swap into XNO")
    s.add_argument("amount", type=float, help="amount of source asset")
    s.add_argument("--asset", default="USDC-BASE", help="source asset w/ network suffix, e.g. USDC-BASE, USDT-SOL (default USDC-BASE)")
    s.add_argument("--execute", action="store_true")
    s.set_defaults(fn=cmd_topup)

    s = sub.add_parser("serve", help="run the Feeless402 paid-API + faucet server")
    s.add_argument("--port", type=int, default=8402)
    s.add_argument("--host", default="127.0.0.1")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("mcp", help="run as an MCP server (stdio) for agent frameworks")
    s.set_defaults(fn=lambda a: __import__("nano_pay.mcp_server", fromlist=["main"]).main())

    s = sub.add_parser("claim", help="claim free starter XNO from a Feeless402 faucet")
    s.add_argument("url", nargs="?", help="faucet base URL (optional with --auto)")
    s.add_argument("--auto", action="store_true",
                   help="cycle the faucet federation list until one grants")
    s.set_defaults(fn=cmd_claim)

    args = p.parse_args()
    try:
        args.fn(args)
    except (WalletError, X402Error, PriceCapExceeded) as e:
        out({"error": str(e)}, code=1)


if __name__ == "__main__":
    main()
