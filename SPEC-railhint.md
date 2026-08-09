# railHint — draft x402 extension for rail negotiation

The canonical, current specification lives at **https://railhint.com**
(draft-railhint-01), with its JSON Schema at
https://railhint.com/railhint.schema.json. This file is intentionally just a
pointer — earlier revisions kept a parallel copy here and it drifted.

Summary of the current draft:

- Carried at `extensions["rail-hint"]` on an x402 v2 HTTP 402 response, as
  `{"info": {...}, "schema": {...}}` — the same kebab-case key and
  `{info, schema}` shape every extension in the official x402 SDK uses. The
  JSON Schema validating `info` ships inline with each declaration.
- `extensions.railHint` (fields flat, from draft-railhint-00) is a
  **deprecated legacy key**; servers migrating from it may emit both keys
  with identical content during transition.
- Advisory only: `accepts` remains the only binding payment terms.
  Discovery, not execution — a hint tells a client where to look, never
  what to run, and a hint that only works when its command is run is out
  of spec.

Reference implementation: this repository (`nano_pay/server.py` emits it;
see a live example at `https://feeless402.com/premium`).
