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
simulation remain checked before signing/submission; dev-watch registration is local.

This reduces serial waits; it is not zero-RPC prediction of a new pool, and
cannot guarantee a same-slot fill if the state/simulation provider is behind.


### Exit monitoring without an entry ACK gate

The 2026-10-29 DrawFi entry audit recorded a 135.342 ms unsigned build,
137.852 ms cumulative simulation completion, then 893.990 ms waiting for
exit-monitor coverage. Earlier fixes woke the WebSocket refresh immediately
and overlapped ACKs with building, but still gated signing on remote coverage.

The current buy path follows `rh_follow_buyer.py::_arm_dev_sell_watch`: register
one order locally before building, retain its dev in the shared transaction
stream, and refresh market sockets in the background. `MarketFeed.arm()` remains
an explicit coverage probe, but execution no longer calls or awaits it.
See the RH-style registration section below for buffering, reconnect repair,
confirmed sale evidence and the remaining inclusion limits.


### Per-transaction priority fee

`SOL_PRIORITY_FEE_SOL=0.0002` is the default total priority-fee budget (200,000
lamports) on every newly built buy, dev exit, take-profit sell and quote sweep.
It excludes the base network fee, account rent and any separately configured
delivery tip. Setting it to zero disables the priority fee. Existing signed
transactions are never modified or re-signed merely to change this setting.

The shared unsigned builder includes a Compute Unit Price instruction in its
single simulation, then derives the final price from the padded CU limit.
Integer rounding never exceeds the configured budget (at most one lamport
below it over supported CU limits). Simulation reserves at least that budget;
the version-2 evidence checks both CU-limit and CU-price arithmetic against the
configured budget while requiring all swap/minOut/accounts/blockhash data to
match. No extra quote RPC or simulation is added by enabling the fee. This
does not enable a Jito/Nozomi tip or switch the submission endpoint.


### Transaction-local Pump / Stonk create builder

`SOL_LOCAL_CREATE_BUILD=true` (default) uses the successful source transaction's
program-attributed create/trade events for the newly created mint and curve/pool.
`features/strategy/create_snapshot.py` verifies a complete invocation stack,
one creation, bounded event data, and rejects unknown launchpad operations or
mint mutations outside creation. Foreign programs' event-shaped log messages
are ignored. These projections belong to one build; they are never inserted
into the shared RPC account cache or reused for subsequent buys/sells.

For supported Pump creates, the builder uses the last TradeEvent's **after**
reserves (or CreateEvent's initial reserves when there was no trade), the pinned
SDK's fee/quote calculation and instruction encoding, plus the existing shared
Global/FeeConfig cache. Stonk uses the decoded initialize parameters and the
last LaunchLab TradeEvent's after reserves. Transfer-fee mint projections keep
the rate and maximum fee from the supported initialize ABI. The event layout
reference is Raydium's official
[LaunchLab IDL](https://github.com/raydium-io/raydium-idl/blob/master/raydium_launchpad/raydium_launchpad.json).

The native Stonk path needs only GlobalConfig/PlatformConfig from the common
cache, with no mint/pool/wallet-balance/epoch RPC. Its WSOL ATA is created
idempotently and retained, rather than closing an account whose prior balance
has not been read. Only the configured SOL amount is wrapped and spent. One
simulation verifies the buy's inner token transfers: exact payment to the quote
vault, net token receipt at least minOut, and no unexpected token transfers.
The actual mint is requested in that same simulation response to reject changed
fee schedules/extensions; there is no additional mint query. Missing inner
instructions or failed simulation reject the trade. Existing WSOL is preserved;
the retained ATA's rent is not reclaimed by this buy.

With common settings and blockhash warm, native builds no longer wait for an
account-read RPC; **one simulation remains**. Non-SOL routes also omit their new
mint/pool reads, but retain quote-hop, existing-balance and ALT checks as needed.
Cold shared settings/blockhash still use the existing bounded RPC fallback.
Incomplete logs, unsupported flags (including Pump Mayhem/holder-reward), unknown
ABI tails, and Stonk creates without a supported trade event use the original
builder. Set the environment switch to false to disable this optimization.

The persistent Node process still encodes instructions; this is not a Python
rewrite or a promise of zero RPC / same-slot inclusion. Signing stays in Python.
Existing minOut/slippage, tax/impact checks, signal age, source validity,
local dev-watch registration, simulation reuse and priority-fee settings remain active.
Successful audit `builder_timing.localCreate` reports `eligible`, `reason` and
`projectedAccounts`, allowing VPS measurements to distinguish local builds from
fallback. The offline `local_create_inputs.json` fixture is derived from the
existing source transaction fixtures; Python tests reproduce it exactly.


For the verified local create path only, continuously subscribed common config
PDAs may be used while the public WS head trails the processed trigger by at
most two slots, provided the stream heartbeat is fresh and subscription coverage
is intact. This avoids re-reading unchanged settings just to advance their
context slot. A larger gap/disconnect falls back to the existing reads. This
exception never applies to mint/pool/quote balances or lookup tables. The single
simulation retains minContextSlot and the original minOut before signing.
All projected mint states (including non-SOL routes) are also checked against
the actual mint returned in that simulation for supply, decimals, supported
extensions and both transfer-fee schedules.


## 2026-10-30 買入等待優化

這次優化保留 processed 來源、dev 數量限制、minOut、滑點、實際 mint／手續費模擬檢查及簽名前的持久化。

- 買入採用下節的本地登記及共用 gRPC 監控，已移除簽名前 WebSocket ACK 等待；市場 WebSocket 仍維持最多 8 個待 ACK 請求，作為背景備援。
- `SOL_EXECUTION_HEDGE_MS=75`（預設）：買入 SDK 模擬及已簽名交易廣播在主端尚未回應時，75ms 後啟動已設定的備援。主端提早回應不增加備援請求；主端立即不可用則即時啟動備援。僅一個端點時維持原路徑。
- 模擬使用第一個有效、符合 `minContextSlot` 的結果，包含有效的模擬失敗；不因失敗再尋找成功答案。資料缺失、落後 slot 或限流不算有效結果。其他帳戶查詢及背景工作維持原有順序備援。
- 廣播兩端使用同一筆已持久化的已簽名位元組，檢查回傳簽名一致。此功能也適用同一 execution lane 的賣出／清掃廣播。中斷 HTTP 等待不代表鏈上取消；所有端點無有效 ACK 時，原訂單仍保持 unknown，由原有 reconcile 查明，不重新簽名。
- 保留每端主網核對、429 通知及共用冷卻。取消競速落後的一端不會把正常端點標成失效。每次競速最多增加一個備援請求，可能增加 Alchemy 用量；不新增 tip、不改 priority fee。

部署後可在原有 `dev_audit` 的 `builder_timing.rpc` 查看每次 RPC 的 `provider`（primary/fallback）、`ms`、`attempt`、`hedged`、`outcome`。這些是建置結果產生前已完成的嘗試；被取消且尚未結束的嘗試可能未列入。沒有端點 URL 或 API key。Health 的 `execution_rpc` 增加 `send_hedges`、`send_primary_wins`、`send_fallback_wins` 累計計數。先比較 `monitor_wait_ms`、`builder_timing.calls.simulateTransaction.ms`、`submit_started_ms` 及 `submit_ack_ms` 的分布，再看 `slot_delta`，不能以 RPC ACK 當作入塊證明。

不需新增 key；沿用 `SOL_EXECUTION_RPC_HTTP_URL`／`SOL_EXECUTION_RPC_FALLBACK_HTTP_URL` 現有解析規則。若要逐步驗證，可先設 `SOL_EXECUTION_HEDGE_MS=0` 只測本地監控登記及診斷，再設 `75` 比較競速；設回 `0` 並重啟即可恢復順序備援。未設定時預設啟用 75ms。背景 blockhash 刷新、訊號有效期限及 slot 新鮮度門檻不變。這是降低本地／RPC 等待，不能保證同 slot。


## 2026-10-30 RH 式本地登記，買入不等賣出訂閱 ACK

收到 create、通過原有入場限制並保留訂單後，`DevSell.register` 只登記該訂單的 dev／ATA，
不掃描所有持倉、不發 RPC、不等待 WebSocket。隨即進入組單、單次模擬、簽名及廣播。
`monitor_wait_ms` 為 0；背景訂閱未完成或斷線不再產生 `dev-sell-monitor-unavailable` 買入拒絕。
minOut、滑點、dev 數量、來源分叉與訊號期限、持久化簽名均保留。

共用 gRPC 的地址為 hotlist 與待買入／現有持倉 dev watch 的聯集。原 hotlist 在 create 時
照常移除，但已保留訂單的 dev 不會因此被取消訂閱；新增 ATA 隨背景 filter 更新加入。
賣出交易通知直接進入持久化 dev-sell inbox，不經 funding worker；入場 live-window 的
丟棄規則不會丟掉既有持倉的賣出通知。無法正常解碼的交易仍以簽名、slot、靜態與 ALT
帳戶提示排入原有 confirmed receipt 驗證。

接收端保留最多 2048 筆、30 秒內的交易帳戶提示，接回 create 解析排隊期間的早到事件。
本地登記會重播相符事件，資料庫用 signature／buy_id 去重。已進 inbox 的事件可跨重啟恢復；
尚未登記的提示只在記憶體中，重啟／斷線間隔由原有有界歷史補查修復。
WS 訂閱與確認、重連補查都在背景執行。gRPC 建立新連線時亦排入持倉的補查。

processed 通知只是賣出候選，不直接當成成功賣出；confirmed receipt 仍驗證 dev 所有權、
實際 venue 轉帳及相同 slot，已知 dead slot 不觸發退出。若組單期間賣出已被確認，原有
簽名前 dev gate 會拒絕買入；若買單已送出，durable dev exit 等待可退出的買入持倉後跟賣。
純 RPC 故障不當作餘額為零。失敗買單及已結束持倉會從背景監控集合清除。

不需新增 .env 參數、API key 或 tip。需部署並重啟服務才生效；本地測試不代表 VPS 實際
入塊結果。此變更消除原本約 865ms 的 ACK 等待項目，但不保證同 slot 成交。
