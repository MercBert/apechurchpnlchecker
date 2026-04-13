# ape-church-tracker

Local indexer + Streamlit dashboard that tracks **every APE** flowing through
ape.church smart-contract games on Ape Chain. First game wired up is
**Blizzard Blitz** (cascade-style slot, native-APE).

Goal: when a player wagers 100 APE, we know exactly where every last bit of
that APE went — player payout, ape.church protocol fee, partner fees, house,
jackpot pool, referral, etc. Any counterparty that isn't labeled yet lands in
an `UNLABELED` bucket that's surfaced loudly in the dashboard, so nothing
slips through uncategorized. Per-block reconciliation against the contract's
on-chain balance delta catches classification bugs immediately.

## How it gathers the data

- **Native-APE games** (Blizzard Blitz): fees and payouts happen via internal
  transactions, which plain `eth_getLogs` can't see. We use the
  [Apescan](https://apescan.io) API (`account.txlist` + `account.txlistinternal`)
  as the source of truth for all value movements, then reconcile per block
  against the contract's native balance delta via `eth_getBalance`.
- **ERC-20 games**: fall back to `eth_getLogs` on the token's `Transfer` event
  filtered to the game contract.
- Contract ABI is **optional** — classification is done by counterparty
  address + direction, not by event semantics. That's good, because Blizzard
  Blitz is unverified on apescan.

## Quick start (Blizzard Blitz)

```bash
pip install -e .
cp .env.example .env
# Edit .env:
#   RPC_URL=https://rpc.apechain.com
#   APESCAN_API_KEY=<get a free key at https://apescan.io/myapikey>

# 1. Sanity-check the reference tx first. This tells you which addresses the
#    fees are going to so you can label them.
python scripts/inspect_tx.py blizzard_blitz \
    0x45c32a21ab454291bfb30f90577c492b9280821eddbc710a30d7bfe550c0ece4
```

The inspect script will:
- Print the tx's wager amount (`msg.value`) and every internal APE move.
- Classify each flow (`wager`, `payout`, `protocol_fee`, `UNLABELED`, …).
- Reconcile `sum(in) − sum(out)` against the contract's block balance delta.
- List every UNLABELED counterparty with a ready-to-paste JSON snippet for
  `config/games/blizzard_blitz.json`.

```bash
# 2. Paste the UNLABELED addresses from step 1 into
#    config/games/blizzard_blitz.json under "labels":
#
#    "labels": {
#      "0x20852d...697da5f51": {"category": "protocol_fee", "name": "ape.church protocol"},
#      "0x258f9ead...d36f98563": {"category": "partner_fee", "name": "partner X"}
#    }

# 3. Re-run inspect to confirm everything classifies.
python scripts/inspect_tx.py blizzard_blitz 0x45c32a21ab...

# 4. Backfill from the deploy block to head (auto-discovered via apescan).
python -m ape_church_tracker.indexer --game blizzard_blitz

# 5. In another terminal, open the dashboard.
streamlit run dashboard.py
```

## Reclassifying without re-indexing

When the dashboard shows `UNLABELED` flows, look the addresses up on
https://apescan.io, add them to `config/games/blizzard_blitz.json` under
`labels`, then run:

```bash
python -m ape_church_tracker.indexer --relabel --game blizzard_blitz
```

This rewrites `flows.category` in place from the stored data — no RPC or
apescan calls.

## Data model (one-liner)

Every row in `flows` is one value movement in or out of the game contract,
tagged with `direction`, `token`, `amount_raw`, `counterparty`, and a
`category` (`wager`, `payout`, `protocol_fee`, `partner_fee`, `house`,
`jackpot_pool`, `referral`, `UNLABELED`, …). Aggregations in the dashboard
roll these up by day, token, and category.

See `ape_church_tracker/db.py` for the full schema.

## Adding another game later

Drop a new `config/games/<game>.json` with the contract address, tokens, and
any known labels. No code changes needed — `load_all_game_configs()` picks it
up on the next run.
