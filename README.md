# nano-pay · Feeless402

Self-custodied Nano (XNO) wallet + x402 payment client **and** merchant
server, built for AI agents. Client spends; `nano-pay serve` earns — paid
endpoints, on-ledger verification/settlement (no facilitator), a starter
faucet, and the `railHint` x402 extension that teaches visiting agents how
to onboard (see SPEC-railhint.md). Site: site/index.html + site/llms.txt.

**The pitch, in one number:** the same $5 buys 5,000 API calls paid as
per-call USDC-on-Base x402 payments (merchants floor prices at 0.001 USDC),
or ~1.8 million calls paid in Nano at the true metered price
($0.0000027/call observed live at nano-gpt.com). Top up once, micropay
forever.

## Install

```bash
pip install -e .          # needs Python 3.10+; pulls nanopy + requests
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
  fallback (~25s), and work for the next block is pre-cached after every
  transaction so steady-state payments are instant.
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

Prototype (v0.1.0). Wallet ops, RPC failover, 402 quote parsing against
live NanoGPT, and NanSwap estimates are tested. First live paid call
pending wallet funding. Not audited — keep only working capital in it.

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
