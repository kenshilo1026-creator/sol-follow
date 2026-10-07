# Alchemy hotlist stream / public execution

Alchemy is used only for the hotlist transaction stream, plus small block metadata
messages needed for chain timestamps, and slot status updates for fork invalidation. CEX/Privacy Cash source logs, HTTP reads,
historical catch-up, SDK quotes, simulations, transaction submission and order
reconciliation use the configured public RPC. There is **no Alchemy HTTP fallback**.
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
Keys in other projects are never read. Existing dry/live and wallet settings are
unchanged. gRPC availability and filter capacity depend on the Alchemy account.

Install `requirements.txt` before starting. Offline `features.app check` reports
the selected feed and whether a key is configured, without printing endpoints or
credentials. It does not prove live gRPC access.

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
- Stream jobs have a separate worker from history/source jobs. Background public
  reads are rate-budgeted and yield while a buy is active. They still share the
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
Each gap covers at most the last 120 seconds (or the shorter configured backfill
age), with one page of 100 signatures per turn and at most three pages per source.
Pagination resumes its saved cursor instead of fetching the newest page again.
Unknown timestamps are not fetched as part of this short window. A full page at
the budget limit is logged as incomplete coverage; long outages are not backfilled.

New hotlist addresses only update the live subscription. Existing queued address
scans for non-source wallets are discarded without RPC calls. On gRPC reconnect,
the client reads the current slot and subscribes without `from_slot`; missed target
buys are deliberately not replayed. Failures remain visible and retry with backoff.
Watch-set updates do not trigger public history requests.

Buying requires fresh in-memory receipt evidence from this process's WebSocket or
a gRPC transaction that passes the current-slot/time anchor barrier. History jobs
and payloads restored after restart cannot vote or trigger buy quotes. Untouched
recovery jobs older than 120 seconds expire before a transaction fetch. Funding
qualification retries retain their existing deadline. The independent 30-day
pre-deposit signed-activity check, processed fork checks and own-order reconciliation
remain enabled; this policy does not remove all historical RPC calls.

Providers do not return a per-address subscription acknowledgement. Events around
filter updates and during disconnection can be missed under this live-only policy.

Health reports stream messages, received protobuf bytes, reconnects, slot and
address count separately from public HTTP calls. Protobuf byte counts are a local
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
deduplication, no idle history calls, public-only routing, live-only reconnection, and an
in-process gRPC server exercising receipt, filter updates and cancellation.

Official references:
- https://www.alchemy.com/docs/reference/yellowstone-grpc-quickstart
- https://www.alchemy.com/docs/reference/yellowstone-grpc-subscribe-transactions
- https://www.alchemy.com/docs/reference/yellowstone-grpc-subscribe-request
- https://github.com/rpcpool/yellowstone-grpc/tree/79abd849f6a7ea2284d861388adf249bd4093d92/yellowstone-grpc-proto
