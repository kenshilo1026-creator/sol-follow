# Hotlist dev create sniping

The active entry trigger is `dev-create`, replacing rolling multi-wallet buys.
This follows token_alert/rh_follow_buyer.py on_launch's core ordering: funding
qualification happens ahead of time, a signing launch wallet consumes its
hotlist on create, then entry filters and the existing executor run. It does
not import RH runtime, credentials, wallets or chain-specific fingerprints.

## Trigger and attribution

A supported successful Pump create/create_v2 or verified Stonk initialize is
required. The signing instruction `user` (launcher/payer) must have qualified,
unexpired CEX/Privacy Cash funding from a strictly earlier slot. An arbitrary
metadata `creator`/fee beneficiary cannot borrow another wallet's hotlist.
Top-level and verified CPI creates work when that launcher actually signed.
PDA-only/unsigned launch users are not eligible.

Creates without any embedded dev buy are supported. Routes use the decoded
mint, curve/pool, quote and token programs: native Pump/Stonk curves for SOL,
and the existing SOL-to-quote engines for non-SOL pairs. No buyer's old calldata
or token amount is copied. The user's size remains `SOL_BUY_AMOUNT_SOL`.

Entry requires an in-process live receipt, a fresh signal after service start,
and usable source proof at the configured commitment. Processed mode requires
a pending live processed proof; confirmed follow-up cannot initiate another
entry. Historical catch-up and restart recovery never snipe an old create.
This is post-observation Solana sniping, not an Ethereum-style pending mempool
or a guarantee of buying in the create's block.

## Durable consumption and execution

The funding database consumes the dev's hotlist atomically before entry checks
or awaited builds. Its existing tombstone records the create signature/slot.
Only the first eligible create wins, including parallel workers and multiple
creates in one signature. Known prior creates from the same launch user reject
entry. A stale launch, filter rejection, failed build/simulation or restart
cannot rearm the consumed create. Later funding does not bypass known previous
launch history. This retains existing history coverage/retention limits.

A processed create is saved provisionally and upgraded on confirmed follow-up.
Only a proven failed create/dead slot restores that create's consumed funding,
if still unexpired. Uncertain/null status does not restore it, and restoration
never overrides a newer consumption or a newer funded hotlist entry.

Synchronous dev launch token quantity checks run before order reservation, along with pool identity,
liquidity, transfer tax, fees, price impact, slippage, source fork checks,
simulation and durable signing. Pending quantity checks never permit a create buy.
Existing signed orders, positions and emergency-exit reconciliation are retained. The original per-mint strategy reservation
is reused so old orders/positions remain protected. Unknown broadcast results
remain reserved; no new signing/rebuy is added.

Ordinary buys/sells no longer collect wallet votes or trigger entries. They can
still supply verified quote-route cache hints. `SOL_FOLLOW_MIN_WALLETS`,
`SOL_FOLLOW_WINDOW_SECONDS`, remaining-position votes and observed buyer
MIN/IGNORE/MAX amount settings no longer gate this strategy; legacy fields
remain parseable for compatibility. `check` and startup display
`buy_trigger: dev-create` instead of wallet threshold/window metadata.

## Deployment and verification

Deploy the Python changes and restart `sol-follow`. Keep `.env`, wallets and
existing databases; startup uses additive/existing schema and does not reset
orders or hotlists. This change does not turn off DRY_RUN or change the private
key. No live service/transaction is started by the tests.

Offline regressions cover pure create entries, signing attribution, earlier
funding, expiry, old/history-only creates, first-launch/dedupe, processed proofs,
confirmed replay, dead-slot restoration, entry gates and dry simulation.

## 2026-10-11: market-cap gate removed

Create entry no longer evaluates market cap, even if the old
SOL_FOLLOW_MAX_MARKET_CAP_USD_K setting or token_entry_caps exclusions remain.
Order reservation bypasses that legacy gate specifically for create entries.
Build/bootstrap/prewarm do not enable USD market-cap or observed-buy oracles;
the old warm_limit background operation is no longer scheduled. Quote-route
building, pool/transfer fees, price impact, liquidity and slippage still apply.

## 2026-10-11: local dev quantity decisions before entry

The active service uses CreateDevGate, not the background DevHoldingsGate.
SOL_DEV_MAX_HOLDING_TOKENS now caps dev launch token exposure: tokens received
in the create transaction, including tokens subsequently transferred away,
sold or burned in that same transaction. It is not a current wallet-wide RPC
snapshot. The actual signing launch user is checked; an arbitrary creator/fee
beneficiary field does not borrow another wallet's hotlist or change this role.

Verified Pump buy/buy_v2 exact-output calldata can immediately prove an
over-limit rejection. Otherwise, the gate checks the already received complete
transaction: fresh-mint token balances plus identifiable dev outflows, burns
and internal transfer fees. Exact-in calldata's minimum output is never treated
as actual tokens received. Token program, owner, decimals, account indices,
calldata bindings and signature/slot must agree. Missing/ambiguous metadata,
unidentified outflow owners or unsupported nonzero Stonk vesting skip entry.
No account/mint/history RPC is added by this check.

The decision (allowed/blocked/unavailable), quantity source, raw units and
create signature/slot are committed before reserve/build/simulation/sign/send.
checking states and legacy RPC evidence cannot authorize a create order.
Order reservation and the executor recheck this local proof. Unknown results
are never zero tokens; a later confirmed payload does not rearm consumption.
Equality with the configured limit remains allowed; a blank limit retains the
existing explicit opt-out for new quantity checks.

Nine confirmed mainnet samples queried on 2026-10-11 are saved as public JSON
fixtures in tests/fixtures/create_amount_live. This includes three exact-input
Stonk examples whose minimum outputs differ from real receipts. The largest
sample (~95.34m tokens) is rejected before an order at the 60m setting. Tests
also cover boundaries, missing data, token transfers/burns, restart and zero
extra holding RPC. Normal quote construction, simulation, signing and existing
funding reads retain their own RPC requirements.

## Dry-run create notifications

In DRY_RUN, every supported create decoded from transactions processed by the
service queues a Telegram alert, including processed provisional receipts,
non-hotlisted launchers, pure creates and entries rejected by quantity gates.
Notifications run before funding qualification and entry filters. They do not
expand the feed into a whole-network create subscription. The alert identifies
the mint, signing launch user, transaction, slot, instruction and commitment;
processed receipts are explicitly marked unconfirmed.

Event IDs are persisted separately from launch eligibility, so processed to
confirmed follow-up, job retries and restarts do not queue the same create
again. Different creates in one transaction each notify. Deduplication records
are retained for at least 24 hours and twice the transaction backfill window;
older transactions are already outside processing coverage. A full notification
queue releases its claim and retries the job instead of silently losing the
alert. Normal Telegram credentials and delivery error handling still apply.
Live mode retains its existing notification behavior. No notification changes
entry rules or enables transaction submission in dry mode.
