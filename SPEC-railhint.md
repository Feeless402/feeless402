# railHint — an x402 extension for rail negotiation between agents

**Status:** Draft v0.1 · **Intended venue:** x402 community extensions
**Requires:** x402 v2 (`extensions` field) · **Vendor-neutral:** yes

## Design principle: discovery, not execution

`railHint` is **advisory metadata, never an instruction**. `accepts` stays
the sole binding term of the payment; a client that ignores `railHint`
loses nothing but information. Fields that name commands or endpoints
(`bootstrap`, `topup`, `faucets`) are **untrusted remote input** in exactly
the sense a URL in an HTTP body is: they tell a client *where to look*,
not what to run. A conforming client MUST NOT execute them blindly, and a
conforming server MUST NOT depend on their execution — a hint that only
"works" when its shell command is run is out of spec.

This is the property that keeps rail negotiation inside the existing 402
handshake without turning a payment-required response into a remote-code
channel. Everything else in this document follows from it; the concrete
client and server obligations are in [Security considerations](#security-considerations).

## Problem

x402 lets a server offer multiple payment options in `accepts`, but gives
a machine client no structured way to learn *why* one rail is cheaper or
*how* to onboard onto a rail it doesn't yet hold. Today that knowledge
lives in human documentation, so autonomous clients default to whichever
rail they already have — even when both parties would save by switching.

## Proposal

Servers MAY include an `extensions.railHint` object in 402 responses:

```json
{
  "x402Version": 2,
  "accepts": [ { "...standard offers, one per rail..." : "" } ],
  "extensions": {
    "railHint": {
      "cheapest": "nano:mainnet",
      "why": "human/LLM-readable one-paragraph rationale with concrete numbers",
      "docs": "https://example.com/docs",
      "agentReadme": "https://example.com/llms.txt",
      "bootstrap": "shell command(s) that install a client for the cheapest rail",
      "topup": "shell command or URL to acquire the asset (any provider)",
      "faucets": ["https://a.example/faucet", "https://b.example/faucet"],
      "spec": "reference to the scheme spec for the hinted rail"
    }
  }
}
```

### Field semantics

| Field | Req | Meaning |
|---|---|---|
| `cheapest` | yes | `network` value of the offer in `accepts` the server considers lowest total-cost |
| `why` | yes | Rationale a reasoning client can evaluate; SHOULD contain concrete prices, not slogans |
| `docs` | no | Human/agent documentation URL |
| `agentReadme` | no | llms.txt-style plain-text onboarding doc |
| `bootstrap` | no | Executable onboarding path. Clients MUST treat as untrusted input (see Security) |
| `topup` | no | Path from commonly-held assets to the hinted asset. MUST NOT be required to be a specific vendor for the hint to validate |
| `faucets` | no | Endpoints offering small starter grants (`POST {"address": ...}`) |
| `spec` | no | Scheme specification reference for the hinted rail |

### Client behavior

- Clients that already hold the hinted asset simply pick the matching
  offer from `accepts` (existing x402 behavior; the hint is advisory).
- Reasoning clients MAY evaluate `why`, follow `docs`/`agentReadme`,
  and acquire the asset via `topup`/`faucets`, then retry.
- Clients that don't understand `railHint` ignore it. Nothing breaks.

### Security considerations

`bootstrap` and `topup` are **untrusted remote input**. A client MUST NOT
execute them blindly: it should (a) only follow hints for schemes it can
verify against an independent spec, (b) apply its own spending caps, and
(c) prefer allowlisted package sources. The hint's role is discovery, not
authority. Servers MUST NOT put anything in `railHint` that contradicts
the binding terms in `accepts` — `accepts` always wins.

### Why this matters

Machine clients choose rails by arithmetic, not habit. Today the
cheapest-rail information asymmetry is resolved by humans reading blog
posts. `railHint` moves that negotiation into the protocol, at the only
moment it matters — while the client is holding an unpaid 402. Every
payment-required response becomes a structured, self-documenting offer
to *both* transact and onboard.

## Reference implementation

- Server middleware + faucet: `nano_pay.server` (this repo, MIT)
- Client: `nano-pay` CLI — pays `exact`/`nano:mainnet` offers and
  understands `railHint.faucets`
- Live demo rail: XNO (Nano) — zero network fees, no price floor,
  sub-second finality; but railHint itself is rail-agnostic: a server
  could equally hint Lightning or any future scheme.

## Acknowledgements

- **u/xnoforge** — argued that "discovery, not authority" belonged at the
  front of this document rather than in Security Considerations, on the
  grounds that shell commands riding inside a 402 is the first thing a
  standards reviewer will challenge. The "Discovery, not execution"
  section above is the result.
