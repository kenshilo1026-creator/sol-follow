# Alchemy hotlist stream and source-history pagination / public execution

Alchemy is used for the hotlist transaction stream, small block metadata messages
needed for chain timestamps, slot status updates for fork invalidation, and
CEX/Privacy Cash source-address `getSignaturesForAddress` catch-up over HTTP.
Source WebSocket logs, SDK quotes, simulations, submission and own-order reconciliation
use the configured public RPC. Funding transaction details, recipient checks and
funding/processed status confirmation use public RPC first, then at most one Alchemy attempt after errors or invalid/
unindexed results. Source-history pagination remains a direct Alchemy route.
Adding an API key never rewrites `SOL_RPC_HTTP_URL` or `SOL_RPC_WS_URL`.

## Configuration

Add to this project's `.env` (do not paste credentials into chat or commit them):

```dotenv
ALCHEMY_API_KEY=your-key
SOL_FEED_MODE=alchemy_grpc
SOL_HOTLIST_COMMITMENT=processed
SOL_FOLLOW_MAX_TARGET_BUY_SOL=5
SOL_ALCHEMY_GRPC_ENDPOINT=https://solana-mainnet.streaming.alchemy.com
SOL_RPC_HTTP_URL=https://api.mainnet-beta.solana.com
SOL_RPC_WS_URL=wss://api.mainnet-beta.solana.com
SOL_BACKGROUND_RPC_RPS=2
SOL_PUBLIC_RPC_TIMEOUT_MS=2000
```

`SOL_FEED_MODE=auto` (default) selects gRPC if a key is present, otherwise legacy
WebSocket. Explicit `alchemy_grpc` fails configuration validation if no key exists.
`SOL_ALCHEMY_API_KEY` is accepted as an alias; `ALCHEMY_API_KEY` takes precedence.
With either key configured, source pagination uses
`https://solana-mainnet.g.alchemy.com/v2/<api-key>`, including in WebSocket feed mode.
Without a key, legacy source pagination shares the public background client.
When selected, Alchemy failures retain the durable request for retry and never
fall back to the public endpoint. Both background clients use
separate budgets: public `SOL_BACKGROUND_RPC_RPS` (default 2), Alchemy
`SOL_FUNDING_ALCHEMY_RPS` (default 8); cooldown is per endpoint.
Alchemy pagination, qualification fallback and status fallback share that budget.
`SOL_FUNDING_WORKERS` (default 4, range 1–8) enables bounded funding concurrency
in gRPC mode, while create processing keeps its separate worker. Background
requests have at most four HTTP calls in flight per endpoint; the pacing lock
only reserves request starts. Same-wallet qualification remains serialized, and
active signatures are excluded even after the durable 60-second lease expires.
The funding transaction is reused for its own launch-history check. Account and
signature-list results are not cached across funding events. Raising the Alchemy
budget increases request usage; adjust it to the account budget. WebSocket mixed
funding/create mode retains one worker to preserve trade processing order.
Keys in other projects are never read. Existing dry/live and wallet settings are
unchanged. gRPC availability and filter capacity depend on the Alchemy account.

Install `requirements.txt` before starting. Offline `features.app check` reports
the selected feed, `funding_history_provider`, and whether a key is configured,
without printing endpoints or credentials. It does not prove live Alchemy access.

## Data flow

- Public WebSocket subscribes to configured CEX/source addresses only in gRPC mode.
- The Alchemy gRPC filter contains active hotlist addresses, chunked at 10,000 keys
  per named filter. No unfiltered transaction/block subscription is used. An empty
  hotlist closes the stream. Account limits must be verified on the actual account.
- Successful non-vote transactions (processed by default, optionally confirmed) carry the full message, balances,
  inner instructions and ALT keys. The adapter preserves legacy/v0/v1 and feeds the
  existing decoders. It does not expand the supported buy/sell route set.
- Block metadata supplies chain time and a recent live-stream anchor. Processed
  signals without blockTime use an explicitly marked first-receipt timestamp only
  after passing the live-slot barrier, with no getBlockTime wait. Confirmed
  mode can fetch missing blockTime once per slot. Receipt time is not chain time.
- The full normalized transaction is durably inserted alongside its job before
  advancing the checkpoint. Completed payloads are removed. Replays deduplicate by
  signature. A metadata/schema rejection queues a public `getTransaction` fallback,
  with an explicit diagnostic; no paid HTTP call is made.
- Source-address pagination uses its dedicated Alchemy background client when
  configured. Returned signatures still enter the existing durable jobs; their
  `getTransaction` reads and qualification checks use public RPC first with
  bounded Alchemy recovery. Complete streamed transactions are still reused.
- Stream jobs have a separate worker from history/source jobs. Background reads
  are rate-budgeted and yield while a buy is active. Public reads still share the
  provider's IP limits; this cannot guarantee a buy is never rate limited.
- CEX funding admission still requires confirmed evidence. Target buy budgets
  above the configured SOL cap, undecodable budgets, and missing/stale non-SOL
  cached valuations do not vote. Processed sources are durably recorded and
  reconciled in public status batches; dead slots revoke votes and block unsent buys.
  Confirmed follow-up retains event deduplication. This change does not implement first-buy
  retirement or add unsupported buy routes.

## Recovery without continuous polling

There is no round-robin `getSignaturesForAddress` scan when idle. Only configured
CEX/Privacy Cash source addresses receive startup/reconnection gap requests.
Each new gap fixes its cutoff at the preceding 120 seconds (or the shorter
configured backfill age). A source gets at most three pages of 100 signatures per
turn, then yields for about one second if unfinished. This is a scheduling budget,
not a 300-signature coverage cap. Successful continuation resets failure attempts;
RPC failures retain the shared cooldown and exponential backoff.

Each page atomically commits jobs and its before/until cursor. observed_latest
records the gap head; latest advances only when the gap is fully scanned. Saved
cutoffs never slide forward or expire while paging, and restarts resume the same
gap. Previous-format pending cursors retain their original until anchor and head.
Unknown timestamps are excluded from jobs but do not alone end pagination.
Malformed or non-progressing responses fail without moving the cursor.

A new gap still starts with the recent window, so this does not promise complete
coverage of long outages or time spent outside that window between separate gaps.
Existing qualification and queue expiry deadlines remain in force; completing
signature enumeration is not proof that every candidate entered the hotlist.

New hotlist addresses only update the live subscription. Existing queued address
scans for non-source wallets are discarded without RPC calls. On gRPC reconnect,
the client reads the current slot and subscribes without `from_slot`; missed target
buys are deliberately not replayed. Failures remain visible and retry with backoff.
Watch-set updates do not trigger public history requests.

Buying requires fresh in-memory receipt evidence from this process's WebSocket or
a gRPC transaction that passes the current-slot/time anchor barrier. History jobs
and payloads restored after restart cannot vote or trigger buy quotes. Untouched
recovery jobs older than 120 seconds expire before a transaction fetch. Funding
qualification retries retain their existing deadline. The independent transaction-count
and launch-history checks, processed fork checks and own-order reconciliation
remain enabled; this policy does not remove all historical RPC calls.

Providers do not return a per-address subscription acknowledgement. Events around
filter updates and during disconnection can be missed under this live-only policy.

Health reports stream messages, received protobuf bytes, reconnects, slot and
address count separately from public HTTP calls. `funding_history_rpc` reports
the selected provider, calls, errors and rate limits. Direct Alchemy source-history
429 notifications use `Alchemy RPC 429` with source `alchemy-history`, without
credentials. Funding recovery defers Telegram alerts until both providers fail.
Protobuf byte counts are a local
usage indicator, **not an exact Alchemy invoice meter**.

## Bounded live probe

```powershell
.\.venv\Scripts\python -m features.app check
.\.venv\Scripts\python -m features.stream_probe --count 10000 --seconds 15
```

The probe submits deterministic inactive public keys to test filter capacity and
receives at most 200 messages for at most 60 seconds. Add `--wallet ADDRESS` to
include a real watched wallet. It never opens the service database, loads signing
keys, sends transactions or contacts Telegram. Metadata reception demonstrates
transport/filter acceptance only; it does not prove successful wallet buy decoding,
coverage of every event, latency under load or trading readiness. Keys, endpoint
URLs and provider exception details are never printed.

## Protocol source and tests

The Apache-2.0 `yellowstone-grpc-proto` package is pinned at
`79abd849f6a7ea2284d861388adf249bd4093d92`; upstream proto files, package metadata
and license are in `chain_common/yellowstone/proto`. Python code was generated with
`grpcio-tools==1.71.0`, then the two `solana_storage_pb2` imports were made relative.
`grpcio-tools` is only required to regenerate; runtime dependencies are pinned in
`requirements.txt`.

Regression tests include 10,000-key serialization, four real Create fixtures and
the supported buy-route fixture round-tripped through protobuf, restart durability,
deduplication, no idle history calls, Alchemy source-history/public execution routing,
isolated endpoint cooldowns, live-only reconnection, and an
in-process gRPC server exercising receipt, filter updates and cancellation.

Official references:
- https://www.alchemy.com/docs/chains/solana/solana-api-endpoints/get-signatures-for-address
- https://www.alchemy.com/docs/reference/yellowstone-grpc-quickstart
- https://www.alchemy.com/docs/reference/yellowstone-grpc-subscribe-transactions
- https://www.alchemy.com/docs/reference/yellowstone-grpc-subscribe-request
- https://github.com/rpcpool/yellowstone-grpc/tree/79abd849f6a7ea2284d861388adf249bd4093d92/yellowstone-grpc-proto

## Funding public-first recovery (2026-10-10)

`features/funding/rpc.py` applies the token_alert funding routing principle to
Solana: candidate `getTransaction`, recipient `getAccountInfo`, recipient
`getSignaturesForAddress` and missing `getBlockTime` use public RPC first. A
configured local Alchemy key enables one recovery attempt using the existing
Alchemy background client. Each actual request retains rate pacing and waits
for active trades to finish. No key is read from token_alert.

Funding failures and malformed/unindexed results cool that provider for 30 to
120 seconds. HTTP/JSON-RPC 429s also retain the endpoint's existing cooldown.
An empty complete signature list and a null account value are valid responses;
a null transaction, malformed history or stale account context are unavailable.
If both providers fail, the persisted job follows its existing retry/expiry
policy. This change neither resets stopped jobs nor grants admission from an
unknown result. Signature, slot, account and launch validation remain required.

Only the confirmed/non-streamed worker uses the funding reader. A separate status
reader now handles processed-signal reconciliation and funding/create finalization
as described below. The processed live worker, transaction execution and own-order
reconciliation keep their existing RPC clients. Source-address pagination stays on
Alchemy without a public fallback. Health `funding_check_rpc` reports safe
attempt/recovery counts; the Alchemy background client's counts now include
both source-history reads and funding recovery, not just pagination.

For funding reads with Alchemy recovery configured, each provider's actual 429
is still logged/audited, but does not immediately alert Telegram. A valid public
or Alchemy result completes silently. Only when neither provider returns a valid
result does `funding RPC unavailable` alert, at most once per method per 60 seconds
to avoid flooding while providers cool down. Malformed/unindexed recovery results
also count as failures; cancellation does not. The deferral is task-local: direct
source pagination, execution, own-order reconciliation, WebSocket and SDK alerts retain
their existing behavior. Without an Alchemy fallback, public 429 alerts are unchanged.

## Status confirmation recovery (2026-10-11)

Funding/create finalization and processed-signal reconciliation now use
`ConfirmationRpc` for `getSignatureStatuses`, with public-first Alchemy recovery.
The same local Alchemy key is reused. Rate budgets, trade priority and endpoint
cooldowns are shared with the existing background clients; status recovery has
separate failure counters/cooldowns so a missing funding transaction does not
itself disable status queries.

Each batch retains `searchTransactionHistory=true`. Responses must contain a valid
context, exactly one value per requested signature, valid slots, an explicit error
field and a recognized confirmation status for each non-null value. A null value
is valid unknown evidence, not confirmation. Malformed responses trigger recovery
before any local state changes; both failures keep pending checks for retry.
Existing chain-error, slot-mismatch and missing-confirmation vote rules remain.

Recovered 429s only produce local audit/log records. If both providers fail, one
`confirmation RPC unavailable` Telegram notification is emitted per method per
60 seconds. Finalization does not send a second deferred-error alert for that same
outage. Without Alchemy configured, the existing public-error alerts remain.
Health exposes `confirmation_rpc` counters; check/startup reports
`confirmation_alchemy_fallback` without credentials.

RPC reference: https://www.alchemy.com/docs/chains/solana/solana-api-endpoints/get-signature-statuses


## Isolated receiver process (2026-10-29)

The live `HotlistFeed.run()` starts `python -B -m features.funding.grpc_receiver`
using the same virtual environment. No extra service, package or `.env` setting
is required. Deploy both new `grpc_receiver.py` and `grpc_process.py` modules,
then restart the existing service. The read-only diagnostic probe remains a
direct, fixed-subscription comparison.

The child owns the TLS gRPC channel, network reads, ping replies and subscription
writes. The main process supplies full hotlist snapshots over stdin. Changes can
coalesce to the newest snapshot; there is no historical replay. Protobuf updates
return through stdout in arrival order, with the original wall and monotonic
receipt timestamps. The parent still normalizes transactions, commits the durable
inbox before its checkpoint, checks fork state and live deadlines, and trades.
No signing, SQLite access or strategy execution occurs in the child.

The child output queue is capped at 128 queued messages and 32 MiB including the
frame being written. A frame is at most 16 MiB. Kernel/asyncio pipe buffers are
also bounded; these are additional to that queue. If the queue fills, the child
exits. The parent rejects IPC messages delayed over 2 seconds and reconnects to
the live head. It does not pretend that an old message arrived just now, enlarge
the buy deadline, or replay missed buys. Brief stalls can be absorbed; prolonged
main-process overload still causes skipped signals and reconnects.

Health includes `receiver_pid`, latest `ipc_queue_ms` and `delivery_timing.ipc_queue`
percentiles. `last_source_age_s` now measures provider creation to child receipt;
IPC queue time measures child receipt to parent read completion. The stale-stream
notice includes IPC delay too. Compare these two timings after restart to locate
remaining delay. The subscription address count records the requested snapshot,
not an acknowledgement from the remote server.

On cancellation, empty hotlist, stream failure or shutdown, the parent terminates
and reaps the receiver. Closing the parent's stdin also ends the child. Queue
overflow, transport errors and timeouts use sanitized reason/status codes;
credentials are passed on the private stdin pipe, never argv. The child receives
a minimal environment without wallet, Telegram or cloud credentials. It runs as
the same OS user: this is scheduling isolation, not a filesystem security sandbox.


## Account readiness during buys (2026-10-29)

The persistent builder now races its existing account WebSocket updates against
an on-demand HTTP account read. If a complete, valid cache batch becomes ready
first, it continues the build and cancels that HTTP read and its catch-up retries.
Cancellation belongs to that read only: it does not cool down the endpoint,
disconnect WebSocket, or cancel another build. Health quote_cache.wsReadWins
counts batches completed this way. There is no new periodic polling.

Mutable mint/pool accounts still require actual account data at or after minSlot;
a slot heartbeat alone is insufficient. Existing shared-config coverage rules,
post-ACK HTTP baselines, generation invalidation, fees, slippage/minOut and final
simulation remain. Create-derived addresses are hints, not synthetic pool state.
If neither transport supplies fresh data, the build still fails safely. This
also cannot bypass a simulation node that remains behind the target slot.

Buy HTTP slot-lag recovery uses the signal's original expiry, with a shared
catch-up window of at most one second and a 200ms reserve for later work. This
window starts after initial provider attempts; it is not a total build deadline
or a same-slot guarantee. Existing Python freshness checks remain mandatory.
Background/sell requests retain their shorter retry policy. Cooling/rate-limited
endpoints and deterministic simulation failures are not retried by this policy.

Deploy the new sdk/read-cancel.cjs and sdk/catchup-budget.cjs along with their
callers; no new environment setting is needed. Restart the service to reload
the persistent SDK worker. Tests use local fake HTTP/WebSocket servers only.


### Create-first unsigned preparation

Live Pump/Stonk creates now reach the cached executor before the same receipt's
funding qualification HTTP/history work. Existing hotlist/source/quantity and
reservation checks still run first; a wallet funded within the create receipt
does not become eligible for that create. Funding remains part of the durable
job, and its retry cannot reserve another buy for an already consumed create.
The service's fallback executor also uses the persistent cached builder.

For validated create hints, known ALT reads start alongside the first pool/mint
read. Stonk's epoch read also starts there, and native Stonk reuses the wrapped
SOL balance from that first batch for its simulation protection instead of
reading it twice. Prepared reads are scoped to one unsigned build. Any ALT
introduced later by a non-SOL hop is still resolved normally. Background warmups
and sells retain their existing paths. Blockhash is selected from the shared
cache at compilation time; preparation does not extend its lifetime or the
original signal deadline. Pool ownership, slot freshness, taxes, fees, minOut,
simulation and monitor readiness remain checked before signing/submission.

This reduces serial waits; it is not zero-RPC prediction of a new pool, and
cannot guarantee a same-slot fill if the state/simulation provider is behind.


### Immediate exit-monitor subscription refresh

The 2026-10-29 DrawFi entry audit recorded a 135.342 ms unsigned build,
137.852 ms cumulative simulation completion, then 893.990 ms waiting for
exit-monitor coverage. `MarketFeed.arm()` woke the WebSocket loop, but the loop
cleared that event without bypassing its two-second subscription refresh.
Explicit change events now immediately rebuild the desired subscriptions. The
event is consumed before refresh, so changes during subscription sends survive;
the outstanding WebSocket receive remains alive. A buy still waits for real
subscription ACK coverage and retains its original expiry and simulation gates.

This addresses a demonstrated local delay, not a promise of same-slot inclusion.
That sample landed at create+7 slots versus the compared transaction at +2.
The comparison also showed zero priority fee on our buy, which remains a
separate configuration/fee-policy improvement, not part of this wakeup fix.


The RH reference is `rh_follow_buyer.py::_arm_dev_sell_watch`, called before
the Pons V2 send path: it installs a local watch in an existing transaction
feed, rather than waiting for a fresh per-token remote subscription.
Solana's current exit feed uses address-filtered WebSocket subscriptions, so
local registration alone cannot certify remote coverage. `MarketFeed.arm()`
now installs the local consumers synchronously, then returns its awaitable
coverage gate so subscription ACKs and unsigned building proceed concurrently.
Coverage uses each socket's server-ACKed address set (cleared on disconnect),
not the socket's all-addresses-ready flag. A delayed unrelated address no
longer blocks an otherwise covered buy. ACK/removal/disconnect events wake
waiters directly, replacing the 10 ms polling loop. Missing required ACKs
still time out and reject the buy; this does not turn a disconnected stream
into permission to trade. The deadline, simulation and signing rules remain.
