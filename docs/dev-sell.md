# Dev sells: exit 100% of the remaining bot position

For a dev-create entry, any proven sale of that token by the signing launching
wallet triggers a full exit of the remaining recorded bot position. There is no
minimum dev sale amount or proportional-follow setting. A fee-beneficiary creator
field is not substituted for the actual signing dev saved in `dev_sell_watches`.
Existing entries backfill only from matching saved create evidence; old buying
wallets without creator proof are not assumed to be devs.

The existing confirmed WebSocket subscribes to the dev and its token ATA even
when the wallet has left the funding hotlist. Notifications enter a separate
SQLite inbox; a dedicated worker reads the confirmed receipt immediately without
waiting for funding qualification. Unknown receipts retry with bounded backoff,
with a final attempt at/after ten minutes and notification if still unavailable.
A restart resumes persisted work. Watches register before buy submission. Initial
registration and subscription acknowledgements/reconnects trigger independent
confirmed signature catch-up from the create slot (or the last completed cursor).
Catch-up is bounded to 20 pages of 100 signatures and ten minutes of failures;
incomplete coverage emits an alert. Live sale decoding does not wait for catch-up.
Subscription refresh/acknowledgements, RPC,
confirmation, liquidity and an already-pending sell can still delay execution.

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
RPC. Buys still confirming wait for proof of their own fill; unsigned buys are
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
