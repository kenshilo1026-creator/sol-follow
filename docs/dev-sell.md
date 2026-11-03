# Dev sells: exit 100% of the remaining bot position

For a dev-create entry, any proven sale of that token by the signing launching
wallet triggers a full exit of the remaining recorded bot position. There is no
minimum dev sale amount or proportional-follow setting. A fee-beneficiary creator
field is not substituted for the actual signing dev saved in `dev_sell_watches`.
Existing entries backfill only from matching saved create evidence; old buying
wallets without creator proof are not assumed to be devs.

The hotlist gRPC stream also watches position devs and the configured trading
wallet. Normalized successful transaction metadata is decoded immediately at the
configured commitment (normally processed), independently of the buy live window.
The stream's actual transfers and token owners provide the same sale attribution
checks as confirmed receipts. Signature-only WebSocket events, decoding failures
and bounded reconnect history retain the confirmed receipt repair path. Complete
processed evidence atomically records the sale and exit intent without waiting for
that HTTP lookup. Early sales have a bounded compact 30-second buffer for local
watch registration. No raw transaction buffer is added.

Pump sell/sell_v2, Stonk LaunchLab sell_exact_in/out, PumpSwap sell and Raydium
CPMM swap_base_input/output are supported, including nested aggregator CPIs when
the venue directly debits a token account owned by the dev. The decoder verifies
actual SPL/Token-2022 transfers of this mint from that account into the venue's
input vault inside the sell invocation. Simple transfers, burns, failed buys,
failed transactions and another trader's swap cannot trigger this protection.
Router-owned intermediate accounts without direct dev ownership and unrecognized
venues are not inferred as dev sales; those formats need separate attribution.

The proven trigger and exit intent commit atomically. The existing emergency
executor sells 100% of the remaining recorded position, preserving unrelated wallet
tokens. Finalized buys reuse their verified saved fill, avoiding an extra buy-receipt
RPC. Buys still confirming can use their own successful streamed fill: the exact
already signed order signature, signing wallet and positive received token delta
are checked, and the provisional fill is stored separately from finalized position
accounting. It is usable for 30 seconds and excluded if its slot is known dead.
A newly arriving fill interrupts a slow confirmed buy-receipt request. Without
streamed evidence, confirmed HTTP remains the fallback; no quote is treated as
an actual fill. Unsigned buys are
blocked at the pre-sign check after a proven dev sale. The sell's minimum RPC
context includes the dev-sale slot.

Dev exits take priority over new profit exits and share a per-mint lock. An already
signed/pending/unknown take-profit sell must reconcile first; the dev exit then sells
only what remains. Signed exit bytes persist before sending, unknown sends never
create replacement orders, and repeat dev events cannot create duplicate exits.
Completed exits remove their subscriptions. Dry mode records/notifies detection
and skips real selling. No new environment setting or separate API key is needed.

## Manual/external full exits (2026-10-29)

Before announcing a proven dev sale or building an unsigned dev exit, the service
checks the configured wallet's token accounts through the existing public execution
read lane. It also rechecks after `sell-balance-unavailable`, covering a manual sale
that lands while the SDK is building. This is event-driven, not a polling loop.
Each check has a two-second budget; a positive balance requires one RPC read.

A zero must cover every token account for the mint (including closed accounts),
use a finalized context at or after the verified finalized buy, and match the
wallet that signed that buy. Pending buys/sells, signed or unknown dev exits,
changed positions, stale contexts and RPC failures prevent retirement. Finalized
chain state can lag a just-submitted manual sale; unknown evidence keeps the
normal exit/retry behavior rather than pretending the wallet is empty.

Once zero is proven, the position amount becomes zero, unsigned dev exits are
cancelled, pending dev-sale events are ignored, and dev watches are removed.
Historical fills and buy deduplication remain intact. Cleanup is logged locally;
it emits neither a new dev-sale trigger alert nor a sell-failure retry alert.
A partially sold position is retained; insufficient or frozen token accounts
are not treated as zero. Already signed sells still reconcile their original
signature. No new environment configuration is required.


## 2026-11-03 urgent execution path

Dev exits now use the existing persistent CachedBuilder, removing per-sale Node
and SDK cold starts. They build/read/simulate at the configured hotlist commitment.
The exact SDK simulation is reused only after the same message/slot/CU/fee checks
as buys; otherwise Python simulates. Successful simulation permits skipPreflight
on send/rebroadcast. Slippage, minOut, fee limits, balance checks, per-mint sell
serialization and durable signatures are retained. A signed ambiguous sell is
never replaced merely because it is slow.

A known dead sale slot prevents a new signature; the unsigned intent and dev watch
remain for replacement sale evidence. A rollback learned after send cannot undo
the exit. Processed execution accepts this fork risk and does not guarantee same
slot ordering. If a buy has already been broadcast, a dev-sale event cannot revoke
it: an eventual observed fill arms the remaining-position exit.

Audit stages dev-sell and dev-exit retain receive/trigger time, own-fill source,
build/simulation/sign/send timing and builder RPC statistics. Processed trigger
notices are local; user-facing sell notices follow balance checks/submission.
The existing manual-zero check still runs for finalized positions before building;
recent unconfirmed fills skip that check because they are pending buys.

Create buys also overlap a blockhash lookup with independent account/ALT reads.
A prefetched hash is consumed only within one second and for the same commitment;
otherwise the normal cache/RPC lookup is used. Pool reads, simulation and send
retain their original minContextSlot, and Python retains the signal deadline.
This removes a serial wait on cache misses, without promising slot inclusion.

No new .env parameter is needed. Deploy and restart to activate the new callbacks,
shared sell builder and additive processed_buy_fills table. Existing orders and
positions are preserved; no live transactions are sent by the regression tests.


### Shared-cache recovery after fork invalidation

A processed fork generation change still clears all account, epoch and blockhash
contents. It now preserves existing public WS subscription ACK state, schedules
one new shared-config baseline after trading yields, and requires a new live slot
heartbeat before continuous shared reuse. A foreground/WS update may satisfy that
baseline first. A transport disconnect still drops ACK state and waits for a new
subscription; old-generation in-flight reads cannot populate the new cache.
This fixes repeated HTTP misses after a fork without trusting pre-fork data or
adding periodic account polling. It is a demonstrated recovery bug, not proof of
the specific historical 136ms account-read cause.
