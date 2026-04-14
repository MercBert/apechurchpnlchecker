---
name: ape-church-contracts
description: Use this skill whenever working on Ape Church platform code — building revenue dashboards, indexers, analytics tools, fee trackers, leaderboards, or any tool that reads from or interacts with Ape Church's on-chain casino infrastructure on ApeChain. Triggers on mentions of Ape Church, ApeChain casino contracts, fee receiver, house contract, GP/EXP system, Gimbo Points, Balloons/Geez/MOD partner games, or any of the contract addresses listed below.
---

# Ape Church Smart Contract Reference

Ape Church is an on-chain casino platform built on ApeChain. This document is the complete architectural reference for the platform's smart contracts — what they do, how they connect, what data they emit, and how to extract revenue and economic data from them.

## Core architecture

Every contract on the platform looks up its dependencies through a single **GovernanceManager** contract. Game contracts hardcode only the GovernanceManager address; everything else (FeeReceiver, House, ClaimManager, RNG, etc.) is queried at runtime. This means platform components can be swapped out by the owner without redeploying games.

```
                        ┌──────────────────────┐
                        │  GovernanceManager   │  ← single source of truth for addresses + roles
                        │  0x8632...6b8a       │
                        └──────────┬───────────┘
                                   │
        ┌────────────┬─────────────┼─────────────┬────────────┬─────────────┐
        ▼            ▼             ▼             ▼            ▼             ▼
   ┌────────┐  ┌──────────┐  ┌─────────┐  ┌────────────┐  ┌──────┐  ┌─────────────┐
   │ Games  │  │FeeReceiver│  │ House   │  │ClaimManager│  │ RNG  │  │ UserInfo    │
   │(many)  │  │           │  │         │  │            │  │(Pyth)│  │ + EXPMgr    │
   └────┬───┘  └─────┬─────┘  └────┬────┘  └─────┬──────┘  └──────┘  └──────┬──────┘
        │            │             │             │                          │
        │            │             │             │                          ▼
        │            │             │             │                  ┌─────────────┐
        │            │             │             │                  │BoostManager │
        │            │             │             │                  └─────────────┘
        ▼            ▼             ▼             ▼
    Player bet  Fee waterfall  House P&L     Winnings payout
```

## Contract address registry

### Core platform infrastructure

| Contract | Address | Purpose |
|---|---|---|
| GovernanceManager | `0x8632f22e5A921C751CFbBFF92F058A3b11E96b8a` | Roles, addresses, pause switch |
| FeeReceiver | `0xd208520d...697da5f51` (query `manager.feeReceiver()`) | Platform fee waterfall |
| House | (query `manager.house()`) | Tokenized casino bankroll, ERC20-like shares |
| ClaimManager | (query `manager.claimManager()`) | Player winnings payout rail |
| ReferralManager | `0x885482a0840dDb4F1dB8565B116195c009cC070a` | Referral tracking + payouts |
| UserInfo (wager tracker) | (query `manager.userInfoTracker()`) | Per-user wager ledger ("APE Wagered" / wAPE ERC20) |
| EXPManager | `0x8046Ac65d2A077562989B2f0770D9bB40e3078CD` | GP/EXP issuance, levels, spending ("Gimbo Points" / GP ERC20) |
| EXPBoostManager | `0xfD1b7a93d7bA00C5E8a3592bE7642054647bA30D` | All boost calculations (EXP and referral) |
| GameApeVault | `0xf467d9AaFD051A290fB93D3eA67014966816D753` | Funds GP loot shop redemptions |
| Deployer / Ops Wallet | `0x0d69B1D26F56DEE4449f5ED3998B0380aAa2FE40` | Receives 65% of FeeReceiver residual |

### Randomness oracle

| Component | Address | Notes |
|---|---|---|
| Pyth Entropy (RNG) | `0x258f9EAd08d35955FC80678658d9cDFd36f98563` | First-party Pyth oracle, charges per-request fee |
| Pyth ERC1967Proxy impl | `0x36825bf3Fbdf5a29E2d5148bfe7Dcf7B5639e320` | Standard upgradeable proxy |

### Partner system (FeeReceiver-level, registered in `gameToPartner`)

| ID | Partner | Cut | Recipient |
|---|---|---|---|
| 1 | Balloons | 50% | `0x68B1ccD2fc53925eFD90284Fa7Fee662c564cd19` |
| 2 | Geez | 50% | `0x148D3122554eaD2Dea02d3E1a5bB2d5d1e5B27e1` |
| 3 | Cult Quest | 30% | `0x14398d9ac62C2BfB20b594EE08fD972e4be866e9` |
| 4 | Glyders | 30% | `0x88282E766594645867778763fEAC696d4Aaf3A36` |
| 5 | MOD | 30% | `0xAa41806018Ad31b232b15F2c5a4d5de834cAdb86` |

Additional partners can be added post-deployment via `addPartner()` — partner IDs above 5 should be discovered dynamically by reading `partnerInfo(id)` until empty.

### Partner game contracts (registered in `gameToPartner`)

| Game contract | Partner |
|---|---|
| `0xB02b13Adb8eAaFe1F41ec942612C4a4862b74d1D` | Geez (Diggers) |
| `0xaF107530b56F86eCD59F03A93fb5044F32E02ae9` | Cult Quest |
| `0x5B44Ce34300D1b8d32b5A6119f192e3edA74e144` | Glyders |
| `0xC1046a6B4c01512803772b25f72d9f6FF27F94a7` | MOD |
| `0xc936D6691737afe5240975622f0597fA2d122FAd` | Balloons |
| `0x0717330c1a9e269a0e034aBB101c8d32Ac0e9600` | Balloons |
| `0x40EE3295035901e5Fd80703774E5A9FE7CE2B90C` | Balloons |
| `0x17e219844F25F3FED6E422DdaFfD2E6557eBCEd3` | Balloons |
| `0x59EBd3406b76DCc74102AFa2cA5284E9AAB6bA28` | Balloons |
| `0x5E405198B349d6522BbB614E7391bDC4F4F6f681` | Balloons |

### Known game contracts (Ape Church native + new)

| Game | Address | Notes |
|---|---|---|
| ApeStrong | (GAME_ID = 12) | Dial-your-odds, 2% platform fee, no game-level partner |
| Blizzard Blits | `0x64b27c1c69559A795C98958614398dD7195AE1B8` | ~4.35% platform fee, partner registered at FeeReceiver level |

To enumerate all games, query the `allGameIds` array on GovernanceManager and cross-reference against `isGame(address)`.

---

## Per-contract reference

### GovernanceManager

**Purpose:** Single source of truth for platform addresses, role assignments, and the global pause switch. Every other contract calls into this one.

**Key view functions for dashboards:**
- `feeReceiver()`, `house()`, `claimManager()`, `RNG()`, `referralManager()`, `userInfoTracker()` — current addresses for each component
- `owner()` — admin key
- `paused()` — global kill switch state
- `isGame(address)` — verify a contract is an authorized game
- `isAdmin(address)` — admin role check
- `allGameIds(uint256)` — array of registered game IDs

**Centralization notes:** Owner-controlled, single key (no multisig), single-step ownership transfer (no `Ownable2Step`).

---

### FeeReceiver

**Purpose:** Receives platform fees from games and runs the fee waterfall.

**The fee waterfall (in order):**

1. Game calls `takeFee(ref, player)` with the platform fee as `msg.value`
2. **GP discount**: `gpFees = (msg.value × GP_WEIGHT × expBoost × EXP_SCALE) / 1_000_000`, capped at 75% of `msg.value`. This amount stays in the contract (eventually swept to the 65/35 house split).
3. **Referral cut**: `refCut = remainingValue × referralBoost / 1000`
   - If player has a saved referrer, pay them
   - Else if `ref` param is valid AND player's `totalWagered < MAX_WAGER_FOR_NEW_REF (2000 APE)`, set referrer and pay
4. **Partner cut** (FeeReceiver-level): if game is in `gameToPartner`, pay `(remainingValue - refCut) × partnerCut / 100` to the partner's recipient
5. Whatever remains stays in the contract until `triggerETH()` is called (permissionless), which sweeps the balance and splits by `recipients[]` allocation (default: 65% deployer, 35% GameApeVault)

**Critical constants/state:**
- `GP_WEIGHT = 35` (current value — this is the baseline GP discount weight)
- `MAX_WAGER_FOR_NEW_REF = 2000 ether`
- `recipients[]`: `[deployer, GameApeVault]` with allocation `[65, 35]`, totalAllocation = 100
- `boostManager`, `expManager` — addresses for boost/scale lookups

**Key events to index:**
- No custom events on `takeFee` itself — track via internal transfers in transactions where `takeFee` was called
- `triggerETH()` produces transfers to recipients

**Admin escape hatches:**
- `withdraw(token, to, amount)` — pull any ERC20 to any address
- `withdrawETH(to, amount)` — pull any APE to any address
- These bypass the 65/35 split entirely

**For dashboards, track:**
- Total fees in (sum of `msg.value` to `takeFee` calls) — this is `totalFees` state variable
- Distribution to ReferralManager (internal transfers)
- Distribution to each partner recipient
- Sweep events to recipients (deployer + GameApeVault)
- `withdrawETH` calls (admin actions)

---

### House

**Purpose:** Tokenized casino bankroll. Users deposit APE, get minted "House APE" shares (ERC20-like), and the share price floats based on house P&L vs. players.

**Core mechanics:**
- `deposit()` — payable, mints shares = `(totalShares × msg.value) / previousBalance`
- `withdraw(amount)` — burns shares, returns `amount × calculatePrice() / 1e18` APE
- Lock time: 15 minutes default after deposit
- Exit fee: 2% (capped at 10%), 50% of which is reflected to remaining stakers, 50% sent to FeeReceiver
- `houseProfit(GAME_ID)` — payable, called by games when player loses (pool grows)
- `payout(GAME_ID, user, value)` — called by games when player wins, capped at `maxPayoutPerGame` (2.5% of pool by default)

**Safety mechanisms:**
- `MIN_PRICE` floor — pauses PvH games if share price drops below threshold
- `maxPayoutPerGame = 250 / 10000 = 2.5%` of total pool per single bet (game-overridable)
- `enableInGamePausing` — when true, blocks withdrawals while VRF requests are pending
- ApeChain native yield enabled via `OnChainYieldManager.configureAutomaticYield()`

**Key events to index:**
- `Deposit(address, uint256)`
- `Withdraw(address, uint256)`
- `HouseWon(uint256 GAME_ID, uint256 profit)` — house profited from a player loss
- `HouseLost(uint256 GAME_ID, address user, uint256 loss)` — house paid out a winning bet
- `Migrate(address, uint256)`
- `Transfer(from, to, value)` — share movements

**Key view functions:**
- `calculatePrice()` — current share price (APE per share × 1e18)
- `balanceOf(user)` — user's stake in APE terms (applies share price)
- `totalSupply()` — equals `address(this).balance` (the total APE in the pool)
- `totalShares` — total shares outstanding
- `getTotalProfits(user)` — int256, lifetime profit/loss for a user
- `getPriceChangeCount()` and related batch functions — historical share price snapshots logged on every deposit/withdrawal and ~every 8 games

**For dashboards, track:**
- Pool TVL = `address(house).balance`
- Share price over time (use `getEvenlySplitPriceChanges()` or `getApproxAverageEvenlySplitPriceChanges()`)
- House P&L per game (sum HouseWon - HouseLost grouped by GAME_ID)
- Top depositors (rank by `balanceOf`)
- Exit fee revenue → flows to FeeReceiver, contributes to platform fees

---

### ClaimManager

**Purpose:** Centralized payout rail for player winnings. Handles EOA push payments and contract pull payments safely.

**Mechanics:**
- `credit(GAME_ID, user)` — called by games (or House) when a user wins
  - **EOA recipient**: pays directly via `.call{value}`, increments `totalClaimed`
  - **Contract recipient**: accrues to `pendingClaim` for pull-based claiming
- `claim()` — user pulls their pending balance
- `claimAndCall(to, amount, data)` — claims pending winnings AND immediately bets them on a game in one tx (sweeps any remainder to wallet)
- `claimFor(users[])` — admin batch claim

**Key view functions:**
- `pendingClaim(user)` — claimable amount
- `totalClaimed(user)` — lifetime claimed
- `getTotalWonByGame(user, GAME_ID)` — per-game lifetime winnings
- `getTotalWonByGames(user, GAME_IDs[])` — batch query
- `totalValueEarned(user)` — pending + claimed

**For dashboards, track:**
- Total winnings paid out (sum credit calls)
- Per-game payout volume
- Top winners (rank by `totalValueEarned`)
- Pending claims outstanding (track contract balance)

---

### UserInfo (wager tracker)

**Purpose:** Per-user wager ledger. Cosmetically presents as ERC20 `wAPE` for wallet/explorer support but is not transferable.

**Mechanics:**
- `wagered(user, amount, GAME_ID)` — called by games on every bet
- Enforces min/max bet bounds (`minBetAmount = 0.1 ether`, `maxBetAmount = 2000 ether`)
- Forwards to `EXPManager.wagered(user, amount)` for GP issuance
- Tracks: global `totalWagered`, per-user `userInfo[user].totalWagered`, per-user-per-game `gameData[GAME_ID]`

**Key view functions:**
- `getTotalWagered(user)` — lifetime wagered
- `balanceOf(user)` — same as `getTotalWagered` (ERC20 alias)
- `totalSupply()` — global total wagered across all users
- `getGameData(user, GAME_ID)` — `(totalWagered, numGamesPlayed)` for a single game
- `batchGameData(user, GAME_IDs[])` — batch
- `getListOfTotalWageredPaginated(start, end)` — leaderboard pagination
- `paginateAllUsers(start, end)` — user enumeration
- `numUsers()`, `listAllUsers()`

**Events:**
- `Transfer(0x0, user, amount)` — emitted on every wager (mimics ERC20 minting)

**For dashboards, track:**
- Total platform volume = `totalSupply()`
- Wagering leaderboard
- Per-game volume distribution
- New user signups (Transfer from 0x0 to a previously-zero balance)
- Active user count

---

### EXPManager (Gimbo Points / GP)

**Purpose:** Issues GP (Gimbo Points) to players based on wagering. GP is both the level system (`totalEXP`) and the spendable currency (`currentEXP`).

**Mechanics:**
- Called by UserInfo on every wager: `wagered(user, amount)`
- Formula: `exp = (amount × EXP_SCALE × expBoost) / (10000 × WAGERED_PER_EXP)`
- Constants: `EXP_SCALE = 100`, `WAGERED_PER_EXP = 0.1 ether`, `EXP_PER_LEVEL = 10000` (= 1000 APE per level at baseline boost)
- `spendEXP(target, amount, data)` — burn currentEXP and call an authorized consumer (loot shop)
- `transfer(recipient, amount)` — currentEXP can be transferred peer-to-peer (totalEXP unchanged)
- `grantBonusEXP(user, amount)` — admin reward distribution

**Two balances per user:**
- `totalEXP` — lifetime accrued, never decreases, determines level
- `currentEXP` — spendable, decreases when redeemed in shops or transferred

**Key view functions:**
- `getCurrentEXP(user)` — spendable balance
- `getTotalEXP(user)` — lifetime
- `balanceOf(user)` — same as `getTotalEXP` (ERC20 alias)
- `totalSupply()` — global GP issued
- `getLevel(user)` — `totalEXP / EXP_PER_LEVEL`
- `getLevelAndEXP(user)` — `(level, expIntoCurrentLevel)`
- `batchGetLevels(users[])` — leaderboard query

**For dashboards, track:**
- Total GP issued (over time, by user, by game indirectly)
- GP spent in loot shops (currentEXP burns)
- Top earners (totalEXP leaderboard)
- Top spenders (track outflows from currentEXP via spendEXP calls)
- Average level distribution

---

### EXPBoostManager

**Purpose:** Calculates boost values used by both EXPManager (for GP issuance) and FeeReceiver (for GP discount + ref boosts). Two separate boost types.

**Boost #1: EXP boost** (`getEXPBoost(user)`) — used in GP issuance and fee discount
```
boost = BASE_GP_BOOST_RATE (100)
      + holyWaterBoost (Vial + Elixir + Cask consumables, if active)
      + (delegatedNFTs × DELEGATED_NFT_BOOST_RATE [5])

cap: MAX_GP_BOOST_RATE (currently 400, but setter can only set 100-300)
```
Then divided by `EXP_SCALE / 100` if EXP_SCALE > 100 (currently 100, so no-op).

**Boost #2: Referral boost** (`getReferralBoost(user)`) — used in FeeReceiver ref cut
```
boost = BASE_RATE_REFERRAL (200)
      + augmentBoost (staked Runestone augments × 20)
      + runestoneBonus (Missionary Runestone NFTs held × 5)
      + houseBoost (user's house balance × 100 / MAX_HOUSE_BALANCE [100k APE], capped at 100)

cap: MAX_RATE_REFERRAL (500)
```

**Critical math:**
- Baseline EXP boost = 100 → 1x GP earning, 35% fee discount
- Max EXP boost = 300 → 3x GP earning, 75% fee discount (capped)
- Baseline ref boost = 200 → 20% of post-discount fee goes to referrer
- Max ref boost = 500 → 50% of post-discount fee goes to referrer

**Source contract addresses (set in BoostManager state):**
- `missionaryRunestone` (NFT) — `0x5341A9aDbe746Ad90332C215f7f8A074d57Ca145`
- `gimboNFT` — `0x81C9ce55E8214Fd0f5181FD3D38f52fD8c33Ec38`
- `gimbozStaking` — `0x64bf43d2412ec6385c7675B6dFEfeb1F933dc29a`
- `gimbozVial`, `gimbozElixir`, `gimbozCask` — consumable contracts (set post-deploy)

**For dashboards, track:**
- Effective boost distribution across the userbase (query `getEXPBoost` and `getReferralBoost` for each user)
- Players with active consumables
- Players with delegated NFTs (high-engagement segment)
- House-staking referrers (the "growth agent" cohort)

---

### House contract balance / GameApeVault

**Purpose:** GameApeVault (`0xf467d9AaFD051A290fB93D3eA67014966816D753`) receives 35% of all FeeReceiver residual sweeps. It funds APE rewards in the GP loot shop — players spend their GP, the vault pays out APE.

**For dashboards, track:**
- Vault balance (`address(GameApeVault).balance`)
- Inflows (transfers from FeeReceiver during `triggerETH()`)
- Outflows (when players redeem GP in the loot shop)
- Net flow over time (sustainability of the rewards economy)

---

### ReferralManager

**Purpose:** Tracks player → referrer relationships and accrues referral rewards.

Address: `0x885482a0840dDb4F1dB8565B116195c009cC070a`

**Interface (from FeeReceiver usage):**
- `addRewards(ref)` — payable, called by FeeReceiver when a ref earns from a bet
- `getRefForUser(user)` — returns referrer address (zero if none)
- `setReferrer(referrer, user)` — called by FeeReceiver when a new ref is locked in

**Note:** Full source not yet analyzed. Pull from ApeScan if you need claim logic, multi-level referral details, or vesting mechanics.

**For dashboards, track:**
- Top referrers by lifetime earnings
- New referral relationships over time
- Pending vs. claimed referral rewards

---

## GameMasterclass — the game contract template

Every Ape Church game inherits from this base. Custom game logic lives in `_playGame()` and `fulfillRandomWords()`.

**Standard bet flow:**
1. Player calls `play(player, gameData)` with `msg.value = bet + vrfFee`
2. Game calculates `vrfFee`, `platformFeeAmount`, `amountForHouse`
3. Game calls `_processFee()` → `FeeReceiver.takeFee()`
4. Game calls `_registerBet()` → `UserInfo.wagered()` → `EXPManager.wagered()`
5. Game calls `_requestRandom()` → `RNG.requestRandom()` → `House.randomRequested()`
6. RNG callback calls `fulfillRandomWords()` on game
7. Game calculates outcome, calls `_handlePayout()`
8. `_handlePayout()` routes funds:
   - Player won more than stake: ClaimManager gets stake-equivalent, House pays the delta
   - Player won less than stake: ClaimManager gets winnings, House gets remainder as profit
   - Player lost: House gets entire `amountForHouse` as profit

**Per-game state:**
- `GAME_ID` — immutable, registered in GovernanceManager's `allGameIds`
- `usedGameIds[]` — every game session's unique ID
- `requestToGame` — VRF request → game session mapping

**Events to index across all games:**
- `RandomnessRequested(uint256 gameId)` — bet placed
- Game-specific `GameStarted` and `GameEnded` events (custom per game)
- `FulfilRandomFailed` — VRF callback failed (rare, indicates bug or VRF issue)

---

## Two partner payment systems (IMPORTANT)

There are TWO ways a partner can be paid from a game. They're independent and a game can use either, both, or neither.

### System A: FeeReceiver-level (centralized, registered in `gameToPartner`)
- Partner cut is taken from the **post-GP-discount, post-referral residual**
- Partner percentage applies to `(msg.value × (1 - gpDiscount) - refCut)`
- Effective take is much lower than the nominal percentage because the GP discount applies first
- All Balloons/Geez/Cult Quest/Glyders/MOD games use this system
- To detect: check if game address is in `FeeReceiver.gameToPartner`

### System B: Game-level (per-game contract state)
- Game contract has its own `partnerAddress` and `partnerFeeCut` state variables
- Partner cut is calculated as a percentage of the **platform fee, off the top**, before fee even reaches FeeReceiver
- ApeStrong uses this system (40% of the 2% platform fee = 0.8% of wager direct to partner)
- To detect: read game's source code for `partnerAddress` / `partnerFeeCut` variables

### Both systems can coexist on the same game
A game can have a game-level partner cut AND be registered in FeeReceiver's mapping. Always check both.

---

## Fee waterfall math (calibrated)

For a wager amount W on a game with platform fee rate F (e.g., 0.0435 for Blizzard Blits, 0.02 for ApeStrong), VRF fee V, player EXP boost B (default 100), referral boost R (default 200 if player has a baseline ref):

```
vrfFee = V (paid to Pyth Entropy, varies — typically 0.1-0.15 APE per request currently)
totalBet = W - V
platformFee = totalBet × F
amountForHouse = totalBet - platformFee

# In FeeReceiver:
gpDiscount = min(0.75, (35 × B × 100) / 1_000_000) × platformFee
remaining = platformFee - gpDiscount

# Referral (if player has a referrer)
refCut = remaining × (R / 1000)
postRef = remaining - refCut

# Partner (if registered in gameToPartner)
partnerCut = postRef × (partnerPct / 100)
residual = postRef - partnerCut + gpDiscount   # gpDiscount stays in contract too

# triggerETH() sweep
opsCut = residual × 0.65
vaultCut = residual × 0.35
```

**Quick reference percentages of the platform fee:**
- Casual player (B=100), no ref: 35% to GP discount sweep, 65% to partner+ops+vault
- Casual + baseline ref (R=200): 35% to discount, 13% to ref, 52% to partner+ops+vault
- Whale (B=200) + ref: 70% to discount, 6% to ref, 24% to partner+ops+vault
- Maxed (B=300) + max ref (R=500): 75% to discount, 12.5% to ref, 12.5% to partner+ops+vault

---

## Revenue sources (everything to track for dashboards)

### Platform revenue
1. **Platform fees** — `takeFee()` calls into FeeReceiver, ultimately split into:
   - 65% to deployer/ops wallet (`0x0d69...FE40`) on every `triggerETH()` sweep
   - 35% to GameApeVault (`0xf467...D753`) on every sweep
   - Deductions before sweep: ref payouts, partner payouts, GP discount portion (also goes to sweep though)
2. **Exit fees from House** — 1% of every withdrawal (50% of the 2% exit fee) flows to FeeReceiver and joins the same waterfall
3. **House P&L** — net of `HouseWon` minus `HouseLost` events; this is depositor revenue, not platform revenue, but worth tracking

### Partner revenue
- Per-partner: sum of internal transfers from FeeReceiver to each partner recipient address
- For game-level partners (System B): sum direct transfers from game contracts to partner wallets

### Referrer revenue
- Track inflows to ReferralManager
- Per-referrer: query ReferralManager directly for claimed/pending balances

### Player rewards economy
- GP issued: sum of `Transfer(0x0, user, amount)` events on EXPManager
- GP spent: sum `currentEXP` decreases via `spendEXP` calls
- APE redeemed from GameApeVault: track outflows from the vault address

### Infrastructure costs (NOT revenue)
- VRF fees to Pyth Entropy: per-game, paid out of every bet's `msg.value`
- These do NOT go to any Ape Church wallet; they're operational expense to a third party

---

## Useful queries / RPC patterns

### Get the current value of any platform component
```solidity
governanceManager.feeReceiver()  // current FeeReceiver address
governanceManager.house()         // current House address
governanceManager.RNG()           // current RNG (Pyth Entropy)
// etc.
```

### Find which games are registered
```solidity
governanceManager.allGameIds(i)         // iterate the array
governanceManager.isGame(gameAddress)   // verify
```

### Get partner info for an unknown game
```solidity
uint256 partnerId = feeReceiver.gameToPartner(gameAddress);
(string name, address recipient, uint256 cut) = feeReceiver.partnerInfo(partnerId);
```

### Calculate effective fee for a player on a game
1. Query `boostManager.getEXPBoost(player)` for B
2. Query `boostManager.getReferralBoost(referrer)` for R (if applicable)
3. Apply the fee waterfall math above to the game's known platform fee rate

### Track house performance
- Index `HouseWon` and `HouseLost` events from the House contract
- Net house P&L = sum of HouseWon - sum of HouseLost
- Use `getApproxAverageEvenlySplitPriceChanges(numPoints, averageCount)` for share price history charts

### Build a wagering leaderboard
- Use `UserInfo.paginateAllUsers(start, end)` + `getListOfTotalWageredPaginated(start, end)`
- Sort by total wagered descending

### Build a GP/level leaderboard
- Use `EXPManager.batchGetLevels(users[])` against the user list from UserInfo

---

## Important corrections / common misconceptions

1. **GP and EXP are the same token.** EXPManager is the "Gimbo Points" ERC20. There's no separate GP issuance contract. The dual-balance design (`totalEXP` for level, `currentEXP` for spending) gives you both currencies in one ledger.

2. **The 35% baseline GP discount applies to EVERY player**, not just whales. Even a brand-new player with boost=100 has 35% of their fee siphoned off into the house sweep before partners or referrers see it. This means partner "50% of fee" cuts are actually 50% of (1 - 0.35) = 32.5% of fee in practice.

3. **House `payout()` only pays the DELTA above the player's stake**, not the gross win. The original stake recycles within the game contract and goes back to ClaimManager directly. This means house bankroll exposure per bet is smaller than gross payout numbers suggest.

4. **VRF fees paid to Pyth Entropy are NOT platform revenue.** They're a third-party infrastructure cost. When auditing fee flows, exclude transfers to `0x258f9EAd...` from revenue calculations.

5. **`triggerETH()` is permissionless.** Anyone can call it to sweep accumulated fees from FeeReceiver. Don't assume sweep timing follows a schedule — it can happen at any block.

6. **The owner can withdraw from FeeReceiver but NOT from House.** FeeReceiver has `withdrawETH(to, amount)` for the owner. House has no equivalent — APE in the house can only move via deposits, withdrawals, or game payouts. This is a meaningful asymmetry for security analysis.

7. **Different games have different platform fee percentages.** ApeStrong = 2%, Blizzard Blits = ~4.35%. There's a 5% hard cap enforced in setters but no platform-wide standard. Always read the game contract or trace a transaction to determine actual fee percentage.

8. **Partner addresses can be splitter contracts.** A "partner address" in FeeReceiver may forward incoming APE to multiple downstream wallets via its own `receive()` function. The platform has no visibility into these downstream splits.

---

## Centralization risk catalog (for honest disclosure)

- **GovernanceManager owner** is a single key, can:
  - Pause all games globally
  - Swap any platform component address (RNG, House, ClaimManager, FeeReceiver)
  - Add/remove games from the whitelist
  - Transfer ownership in a single step (no `Ownable2Step`)
- **FeeReceiver owner** can:
  - Withdraw any APE or ERC20 to any address (`withdrawETH`, `withdraw`)
  - Add/remove/reweight recipients
  - Add/remove/modify partners
- **House owner** can:
  - Set `MIN_PRICE` (pause PvH games)
  - Set `publicStaking = false` (whitelist mode)
  - Set per-user `isFeeExempt`
  - Set the migrator (in theory, a malicious migrator could exfiltrate funds)
  - Cannot directly withdraw native APE from the house pool
- **EXPManager / BoostManager owner** can:
  - Adjust EXP_SCALE (throttles entire GP economy)
  - Adjust boost rates and caps
  - Grant bonus EXP to users (`grantBonusEXP`)

For a public-facing risk page or pitch deck, the standard disclosure is "single-key ops wallet with planned migration to multisig + timelock."
