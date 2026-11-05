# 買入報價與帳戶讀取診斷

新版 execution audit 的 `detail.build_diagnostics` 會在成功送出及組單失敗時保留資料。
沿用 `features.audit.dev_audit wallet <DEV> --hours 24 --json` 查看。
所有記錄均來自既有組單／快取／RPC 結果，不新增 RPC、不重試失敗模擬、不改滑點。
舊交易無法回補當時未記錄的值。

- `quote`：Pump 原生 SOL 路徑實際使用的 SOL 最小單位買額、quoted_out、min_out、slippage_percent、mint_supply，以及 virtual/real token/quote reserves。不是失敗後重查的現價。
- `source_slot`、`local_create`：來源 slot、本地 create 推算是否可用及不可用分類。
- `reads[].phase=projection`：本地推算的 mint/pool、共用快取，或交給帳戶快取讀取的地址。
- `reads[].phase=shared`：共用設定快取判斷；這個階段本身不發 RPC。
- `reads[].phase=cache`：每個地址的角色、命中／未命中、原快取 slot/age、要求 slot、TTL、連續訂閱及 WebSocket 健康狀態。只有 cache-miss 的地址才交給既有讀取流程。
- `reads[].outcome`：cache-hit、http、websocket 或 unavailable；`ms` 是等待既有 HTTP／WS 競速的時間，`response_slot` 是勝出回覆 slot。http 不代表最後所有資料都來自 HTTP，較新的 WS 資料仍優先。
- `rpc`：主／備 RPC 的 method、attempt、hedged、outcome、ms；包含現有 transport 競速與重試，以及失敗分類、RPC code、要求／回報 slot（有提供時）。取消中的失敗競速可能尚未出現在快照內。

快取原因包括 absent（沒有資料）、slot-behind、expired、not-continuous（沒有建立連續更新基線）、subscription-not-ready、websocket-unhealthy、head-behind。共用設定有多項不合格時，reason 表示先遇到的條件，應一起看其餘旗標。

資料有筆數與大小上限，Python 只保留允許的欄位、數字及公開地址；不保留 API URL、金鑰、原始帳戶資料或任意 provider 錯誤文字。
組單超時而 worker 未回覆時，仍沿用既有 progress 診斷，未完成的詳細報價不能保證取得。

6042 / BuySlippageBelowMinTokensOut 需對照 simulation_details、quote 及讀取來源。
僅憑同 slot 無法斷言模擬與報價看到完全相同的池狀態；失敗時也不應把 orders.min_out 的初始 0 當成實際指令參數。


## Create 快速組單與非 SOL 背景快取

Pump／Stonk 的有效 create 推算可直接重用共用設定及報價幣 mint 快取；
這些帳戶不再因快取 slot 早於新 token 的 create slot 而一律重讀。
仍須通過原有 TTL（預設 2 秒），或已建立連續基線、訂閱成功且 WS 健康的條件。
斷線／分叉會清除快取。這不是永久使用舊帳戶狀態。

非 SOL 路徑按報價幣去重背景預熱 SOL→quote 的池、mint、tick/bin 帳戶及 ALT，
不依賴舊目標代幣是否已畢業。已見過的報價幣與路徑持久保存；啟動／worker 更換後恢復最近 128 個報價幣的預熱，
活躍帳戶仍受既有容量限制。新出現或再次觀察到的報價幣會加入預熱佇列。
訂閱 ACK 後以背景讀取建立連續基線，之後使用公共 WS 更新，沒有新增定時 HTTP 刷新。
預熱與基線讀取沿用交易優先 gate；正在進行的單次 RPC 不會因此變成可撤回的請求。

SOL→quote 報價使用這些有效快照，不要求所有池狀態達到新 token 的 create slot。
`reads[].scope=quote` 標示這部分。minOut、滑點／價格影響限制、帳戶 owner 檢查及最終交易模擬仍保留。
冷快取、過期、訂閱中斷、未知路徑，以及新目標 ATA／自身餘額等檢查，仍可能需要 RPC；
因此不能把整筆非 SOL 買入視為保證零 RPC，也不能保證同 slot 成交。


## 非 SOL 快取持久化位置

使用現有資料目錄的 `funding.sqlite3`，無需額外 JSON 或新的 .env 設定：

| 資料表 | 保存內容 |
| --- | --- |
| `seen_quote_mints` | 曾見過的報價幣地址、Token Program、觀察時間 |
| `quote_recipes` | SOL→報價幣的路徑版本、各跳池地址／輸入輸出 mint、ALT 地址 |
| `seen_non_sol` | 曾見過的目標代幣及原始路徑提示 |

已發現路徑會在 worker 回報時寫入資料庫，後續組單／背景預熱自動注入該路徑。
重啟不需重新探索已知路徑；池狀態和價格則背景重新取得，再建立 WS 更新。
持久路徑仍須通過 SDK 的路徑及帳戶驗證。沒有把舊 quoted_out、minOut 或已簽交易當成可重放快取。
最近 128 個報價幣的啟動預熱上限只限制活躍工作量，不會刪除其他持久路徑。
正常重啟或部署時保留資料目錄即可；刪除／更換該資料庫會失去運行期間累積的快取。


## Shared-setting recovery and completed-curve notices

Pump simulation error 6005 is shown as a dedicated “曲線已完成，跳過買入”
Telegram message when the failed program is Pump and the buy is unsigned and
not submitted. This replaces the generic failure text for that alert, without
sending a duplicate notification. Audit retains `buy rejected` and the original
simulation evidence. A simulation error alone does not establish same-slot
completion or completed liquidity migration.

The worker pins the three fixed protocol settings (Pump Global, Pump FeeConfig,
Stonk platform) against ordinary route-cache eviction. Registration happens
before blockhash bootstrap so a failed blockhash read cannot prevent account
subscription and recovery. Other quote accounts remain bounded and evictable.

Shared accounts missing a continuous post-subscription baseline are repaired in
the background, in batches of at most 64 accounts, one batch at a time, with exponential retry
backoff from 1 to 30 seconds. Repairs yield to the trading gate, use an independent
background RPC budget and are deduplicated per account. Successful continuous
snapshots do not cause periodic HTTP refresh. A real disconnect waits for a new
subscription ACK; a socket error without close re-baselines existing subscriptions.
Fork invalidation and worker replacement also restore coverage. Freshness,
source-slot simulation, minOut and slippage checks remain in force.

Account diagnostics now include `pinned`, `recovery_pending`,
`recovery_failures`, and `last_loss` (`never-loaded`, `evicted`,
`subscription-gap`, `baseline-failed`, `websocket-close`, `websocket-error`,
`generation-invalidated`). They identify future cache-loss paths; the older
incident's absent rows alone cannot establish which path occurred.


## Account-read usage (2026-11-05)

A post-ACK `null` account is now a valid absent-account baseline. It stays
subscribed for creation updates instead of triggering a perpetual 30-second
HTTP retry. Foreground source-slot/freshness checks still apply; callers still
reject missing required pool/mint accounts. ACK recovery is coalesced into
batches of up to 64 addresses, reducing 130 single-account requests to 3 in the
controlled test. Transport failures retain backoff and trading priority.

The worker publishes in-memory statistics every 60 seconds, without RPC.
`health.cache_reads` (full health: `quote_cache.accountReads`) contains cumulative
`demand_batches/accounts`, `warm_batches/accounts`, `repair_batches/accounts`,
`null_baselines`, `invalidations`, `evictions`, `subscription_gaps`.
Counters reset when the Node worker restarts. Batch counters count logical cache
HTTP attempts, not Alchemy CU or per-provider retries/hedges, and do not cover
Python or SDK reads bypassing AccountCache. Compare consecutive health records
and the Alchemy dashboard over the same interval to assess savings.


## WebSocket interruption during execution

A socket close/error still clears live account snapshots and continuity.
The validated shared-setting fallback described below is retained separately. It no longer
cancels an independent foreground HTTP read solely because the socket changed.
Only a complete HTTP batch at or above the original required slot may finish
across that gap; it is not installed into the new cache generation. Mixed old
cache hits, forced background baselines, and old-slot replies remain rejected.
The existing HTTP deadline, retries, simulation and minOut checks are unchanged.
No extra RPC or longer buy deadline is introduced.

Processed-fork/explicit invalidation and cache shutdown still cancel reads and
reject the build. A successfully simulated unsigned build may survive a socket
interruption; other worker operations retain their generation guard. Background
recovery establishes a post-ACK baseline before reusing continuous snapshots.
Registered protocol settings can also recover while their socket is offline,
as described below.

`cache_lifecycle` records start/end cache and execution generations plus the last
invalidation reason. Execution generation changes indicate hard invalidation,
even if a later socket event overwrote `last_invalidation`. The last reason is
not a full event history. `reads[].continued_after_disconnect=true` identifies
fresh HTTP results accepted across a socket interruption. Older audits without
these fields cannot distinguish the final interruption cause reliably.


An account WebSocket close/error also no longer discards the independent HTTP
blockhash snapshot or its in-flight refresh. The existing 5-second default TTL,
32-slot distance limit, simulation and submission context checks still apply.
Explicit fork invalidation and worker shutdown still clear it. This removes
avoidable blockhash misses without introducing extra background RPC; it does not
restore missing shared-account subscriptions or guarantee same-slot execution.


## Speed-first shared settings (RH-style last-known-good)

Verified create builds may reuse the last successfully decoded Pump Global and
FeeConfig, Stonk PlatformConfig and the source create's Stonk GlobalConfig when
WebSocket coverage is interrupted or its live row expires. These are separate,
bounded in-memory snapshots; they are not persisted across worker restarts.
Fixed core settings remain pinned. The fallback requires a registered setting,
the expected owner and account layout; an observed newer deletion or invalid
owner/layout revokes it. An older reply cannot resurrect that revoked setting.
Explicit fork/generation invalidation and shutdown clear these snapshots.

The create path does not await subscription recovery or HTTP revalidation when
these snapshots exist. Account evidence records `reason=last-known-good`,
`last_known_good=true`, `row_slot` and `age_ms`. This policy deliberately accepts
that a protocol setting could have changed during the gap, as RH does for common
configuration. Simulation, minimum output, slippage and transfer checks remain;
a stale setting can still cause simulation/on-chain rejection. Cold starts with
no successful snapshot still need an initial read.

Pool reserves, quote/base token mints and their transfer fees, wallet balances,
and price oracles do not receive this stale fallback. Non-SOL conversion routes
continue using their existing background-warmed quote caches and freshness
checks. The setting fallback is used only by verified local-create projection,
not generic account reads or sell quoting.

Recovery batches up to 64 accounts and yields to active trading. While a setting
has no acknowledged subscription, a successful HTTP recovery schedules the next
background attempt after 30 seconds; failures back off from 1 to 30 seconds.
A new subscription ACK immediately requests a fresh baseline. Once continuous
coverage is established, updates use WebSocket and no periodic HTTP refresh is
added. This targets avoidable foreground account reads; it cannot guarantee
same-slot inclusion or eliminate simulation and network delivery time.
