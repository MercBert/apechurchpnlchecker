# ape-church-tracker

Local indexer + Streamlit dashboard that tracks **every APE** flowing through
ape.church smart-contract games on Ape Chain. First game wired up is
**Blizzard Blitz** (cascade-style slot).

The core idea is *flow-first accounting*: for every transaction that touches
the game contract we record every ERC-20 `Transfer` and every native-APE move
in or out, then classify each outflow by destination (player payout, ape.church
protocol fee, partner fees, house, jackpot pool, referrals, …). Any
counterparty that isn't in the config lands in an `UNLABELED` bucket that's
surfaced loudly in the dashboard, so nothing slips through uncategorized. A
per-tx reconciliation compares the sum of flows to the contract's on-chain
balance delta, so classification bugs are caught immediately.

## Stack

- **Python 3.10+**, `web3.py` for RPC
- **SQLite** (single file `pnl.db`) via SQLAlchemy Core
- **Streamlit** for the dashboard
- Runs entirely on your machine

## Quick start

```bash
pip install -e .
cp .env.example .env
# edit .env: set RPC_URL

# 1. Paste the Blizzard Blitz contract address + ABI + known destination
#    addresses into config/games/blizzard_blitz.json and
#    config/games/blizzard_blitz.abi.json. See the example file for the
#    expected shape.

# 2. Sanity-check against the reference tx before backfilling.
python scripts/inspect_tx.py blizzard_blitz \
    0x45c32a21ab454291bfb30f90577c492b9280821eddbc710a30d7bfe550c0ece4

# 3. Backfill + start live tail (keep this running).
python -m ape_church_tracker.indexer

# 4. In another terminal, open the dashboard.
streamlit run dashboard.py
```

## Reclassifying without re-indexing

When the dashboard shows `UNLABELED` flows, look up the counterparty addresses
on https://apescan.io, add them to the `labels` section of the game's config,
then run:

```bash
python -m ape_church_tracker.indexer --relabel
```

This rewrites `flows.category` in place from the stored data — no RPC calls.

## Data model (one-liner)

Every row in `flows` is one value movement in or out of the game contract,
tagged with `direction`, `token`, `amount_raw`, `counterparty`, and a
`category` (`wager`, `payout`, `protocol_fee`, `partner_fee`, `house`,
`jackpot_pool`, `referral`, `UNLABELED`, …). Aggregations in the dashboard
roll these up by day, token, and category.

See `ape_church_tracker/db.py` for the full schema.
