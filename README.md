# nano-pay · Feeless402

<!-- mcp-name: com.feeless402/nano-pay -->

Self-custodied Nano (XNO) wallet + x402 payment client **and** merchant
server, built for AI agents. Client spends; `nano-pay serve` earns — paid
endpoints, on-ledger verification/settlement (no facilitator), a starter
faucet, and the `railHint` x402 extension that teaches visiting agents how
to onboard (see SPEC-railhint.md). Site: site/index.html + site/llms.txt.

**The pitch, in one number:** the same $5 buys 5,000 API calls paid as
per-call USDC-on-Base x402 payments (merchants floor prices at 0.001 USDC),
or hundreds of thousands of calls paid in Nano at true metered prices
($0.0000096/call observed live at nano-gpt.com, confirmed on-ledger).
Top up once, micropay forever.

## Install

```bash
pip install feeless402    # needs Python 3.10+; pulls nanopy + requests
# (from a checkout: pip install -e .)
nano-pay init             # creates ~/.nano-pay/wallet.json (chmod 600)
```

## Agent flow

```bash
nano-pay topup 5                    # quote: $5 USDC-BASE → ~12.3 XNO (NanSwap)
nano-pay topup 5 --execute          # create order (set NANSWAP_API_KEY)
#   → send USDC to the returned deposit address; XNO arrives in 30-60s
nano-pay receive                    # pocket incoming XNO
nano-pay quote https://nano-gpt.com/api/v1/chat/completions \
    --json '{"model":"gpt-5-nano","messages":[{"role":"user","content":"hi"}]}'
nano-pay pay   https://nano-gpt.com/api/v1/chat/completions \
    --json '{"model":"gpt-5-nano","messages":[{"role":"user","content":"hi"}]}'
```

`pay` handles the whole x402 handshake: request → parse the 402 quote
(both the x402nano/`PAYMENT-REQUIRED` v2 dialect and the NanoGPT/`accepts`
v1 dialect) → price-cap check → sign a send state block locally → retry
with `PAYMENT-SIGNATURE` + `X-PAYMENT` headers. The server settles the
block via its facilitator; nothing is broadcast unless the server accepts.

## Design notes

- **Self-custody:** seed never leaves `~/.nano-pay/wallet.json` (0600).
- **No node required:** public RPC failover (rpc.nano.to, somenano,
  rainstorm.city, nanoslo); all signing and PoW happen locally (nanopy
  C extension). RPC `work_generate` is tried first, local PoW is the
  fallback (~25s). The CLI pre-caches work for your *next* block after it
  has printed the result of the current one, so steady-state payments are
  instant. Library callers opt in: `send()`, `receive_all()` and
  `request_with_payment()` take `prework=` and default to **off**, because
  pre-caching blocks for minutes and must never sit on a request path.
- **Safety rails:** per-payment price cap (`--max-xno`, default 0.05);
  `quote` command inspects any endpoint's price without paying;
  balance is always re-synced from the network, never trusted locally.
- **Top-ups without custody:** `topup` quotes/creates swaps directly with
  NanSwap's API (1,400+ input assets, ~$0.02 minimum). This tool never
  holds or routes funds.

## Fork-hazard note (x402 payments)

An x402 payment signs a block the *server* broadcasts. If the server
errors after receiving the block, it may still settle it late. The wallet
re-syncs its frontier from the network before every operation, so a late
settlement is picked up naturally; a competing block signed in the
meantime simply makes one of the two invalid (funds are never at risk,
but don't fire concurrent payments from one wallet).

## Status

Beta (v0.2.2). Proven on mainnet with real funds: live paid calls to
NanoGPT ($0.0000096/call, confirmed on-ledger), full merchant loop
(verify → settle → confirm, no facilitator), PoW-gated faucet claims,
and a complete stranger-agent lifecycle (fresh wallet → PoW claim →
paid API call → confirmed) in under 3 minutes. 13-test suite covers the
payment path offline. Not audited — keep only working capital in it.

## Security notes (read before holding real funds)

- **Self-custody**: the seed lives only in `~/.nano-pay/*.json` (0600).
  No tool or log path ever prints it. Back it up offline.
- **Working capital only**: this is beta wallet software. Keep a few
  dollars in it, not your savings.
- **Spend caps**: `pay` refuses quotes above `--max-xno` (default 0.05).
  MCP `x402_pay` enforces the same cap parameter.
- **railHint is advisory**: hints from remote servers are untrusted
  input. This client never executes remote bootstrap strings; it only
  acts on structured offers that pass its own checks, and `accepts`
  always binds, never the hint.
- **Merchant fork guard**: servers cache accepted frontiers and reject
  duplicate-frontier blocks; payer balance/frontier/signature are
  verified against the live ledger before settlement.
- Not audited. MIT — no warranty.
