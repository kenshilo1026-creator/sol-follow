# Solana automatic take-profit

The running service now watches finalized bot positions and executes the configured
`take_profit_rules.json`. Its format matches RH: `profit_percent=300` means +300%
(4x entry), and `sell_percent` applies to the **remaining recorded bot position**.
Unrelated wallet balances are never included. The user's current RH file was copied
to the sol-follow root; it remains an ignored deployment file. Deploy it separately.
A smaller format example is in `take-profit-rules.example.json`.

```env
SOL_TAKE_PROFIT_POLL_SECONDS=5
SOL_TAKE_PROFIT_RULES_PATH=take_profit_rules.json
```

Take-profit is always enabled; there is no enable/disable environment setting.
Restart after changing rules. A missing file or empty rules produces no new triggers;
invalid rules fail startup. Previously signed sells are still reconciled even without
rules. Dry run neither sells real positions nor invents successful fills.

## Rules and price basis

Fixed profit (`type` omitted, `profit`, or RH's `fixed`), stop loss, fixed profit
protection and trailing drawdown are supported. Protective rules precede profit
levels. Each rule completes only after a validated finalized receipt. A completed
trailing rule rearms when a new observed peak is reached, matching RH. Peaks and
completed rule IDs survive restart. Changing a rule changes its ID.

The trigger follows RH's fill-average / market-spot comparison:

- `entry_index = floor(final_leg_quote_cost * 10^30 / actual_received_tokens)`.
- `spot_index = floor(pool_spot_quote_per_raw_token * 10^30)`.
- `profit_percent = (spot_index / entry_index - 1) * 100`.

The entry index is saved atomically with the finalized buy fill in
`position_market_indexes`, along with its registration time. It is immutable for
that buy. Current and peak spot indexes are persisted separately. Partial sales
and restarts change neither entry index nor registration time. This is a price
index in the original quote asset, not a USD market-cap snapshot. No later live
price is used as an entry price.

Pump curves use virtual quote / virtual token reserves; Stonk curves use
`(virtualB + realB) / (virtualA - realA)`. After graduation, canonical PumpSwap
uses `(quote reserve + virtual quote reserve) / base reserve`; Raydium CPMM uses
quote / base reserves after subtracting accrued protocol, fund and creator fees.
All routes validate pool identity, mints and reserves before accepting the index.
The scale and quote/base denomination stay identical across graduation.

Spot monitoring has no sale amount, liquidation impact or sell-fee deduction.
A partial sale's new pool snapshot is checked against the rule before signing;
its executable quote, fees, token tax, slippage and simulation remain execution
checks. Therefore a lower full-position liquidation quote cannot by itself fire
a stop-loss or move the trailing peak.

SOL routes use the recorded native buy cost; non-SOL routes use the persisted
final-leg exact `quoteIn` with the actual token fill. Network fees, rent and later
quote-to-SOL conversion are excluded. Legacy positions backfill only from saved
buy costs and fills, never current spot. Old non-SOL positions lacking that basis
emit `take-profit-quote-basis-unavailable` and remain retained. Legacy liquidation
peaks are not reused as spot peaks; spot tracking starts from the entry baseline
and subsequent valid observations. Completed fixed-profit rules remain completed.

## Polling

`SOL_TAKE_PROFIT_POLL_SECONDS` defaults to 5 seconds. After 30 minutes from the
persisted registration time, that position's spot polling interval becomes five
times the setting: **25 seconds with defaults** (8 seconds becomes 40 seconds).
Restart and partial selling do not reset the 30-minute clock. Pending signed
transactions continue reconciliation on the base loop interval.

Like RH, a held token's transaction wakes its spot check immediately, bypassing
that token's timer (including the 25-second aged timer). The existing confirmed
WebSocket feed additionally subscribes to held mints and their pools independently
of hotlist membership, including in gRPC mode. It uses the configured funding WS
provider; no new API key or setting is needed. On graduation, the validated spot
reader adds the PumpSwap/CPMM pool to the watch list. Subscription changes refresh
within the feed's two-second refresh cycle, subject to subscription acknowledgements.
The periodic spot read also discovers migration if its event was missed.

Only successful transaction notifications for held accounts wake the worker.
Notifications are coalesced per mint and duplicate signatures are bounded/deduplicated;
events arriving during a check remain pending. The spot RPC uses the event's slot
as a minimum confirmed context, so it cannot evaluate an older pool snapshot.
These events are hints to read validated spot, never prices or permission to sell.
Market-only notifications bypass the funding/create queue. Foreground buys and dev
exits retain priority; interrupted price checks resume after foreground work.
An unavailable RPC defers to later events/polls. Other positions' poll timers and
pending sells' reconciliation continue even when one token trades continuously.
Disconnected feeds and closed positions rely on polling or stop their subscriptions,
respectively; dry run does not add holding subscriptions.

## Execution and recovery

Pump and Stonk curve exits reuse the existing sell builder. Graduated tokens use
its existing PumpSwap/Raydium CPMM migration handling. Unknown routes, unavailable
quotes, invalid balances or fee/tax checks defer the exit. Price polling does not read wallet token accounts,
fetch a blockhash or simulate a transaction. It yields/cancels when a foreground
trade or dev emergency takes priority. Signed sells still retain SDK/Python
simulation and send preflight, along with configured slippage and fee checks.

Only one sell per mint can be reserved. Signed bytes are committed with FULL
SQLite durability before sending. Timeout, missing status and expired blockhash
without definitive failure preserve that signature; retries may only rebroadcast
the same bytes. A finalized chain failure releases the rule for a fresh attempt.
Unsigned interrupted reservations are discarded and reevaluated on restart.

Finalized receipts must match the wallet, signature, sold amount and minimum quote
received. Position reduction, rule completion, order finalization and non-SOL
quote-sweep enqueue happen in one transaction. Quote sweeps use only the received
amount. Dev emergency exits share a mint lock and wait for pending take-profit
sells; after partial profit-taking they sell only the remaining recorded position.

Telegram reports submitted/finalized/unknown/deferred outcomes. Polling every five
seconds is an interval, not a guaranteed execution deadline: RPC, migration,
liquidity and transaction confirmation can delay a stop or profit exit.
