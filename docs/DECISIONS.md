# 實作決策與現行範圍（2026-10-05）

## 現行服務

目前服務啟用 CEX／Privacy Cash 原生 SOL 入金、資格檢查、SQLite hotlist、Pump／Stonk Create 解碼、
WebSocket 即時入口、歷史補查、持久化 jobs／cursor、有限重試、finalized 入金核對、
資料庫容量管理與 Telegram／健康統計。另啟用 Pump 畢業前原生 SOL 買入及非 SOL quote 的
Meteora DLMM → Pump 原子買入、hotlist 計票／預留、模擬及新買單的 finalized 核對。
`check` 與啟動通知列出 `meteora_dlmm_to_pump_curve`／`pump_native_curve`；DRY_RUN 只模擬，LIVE 才讀本地 keypair 簽名廣播。
Stonk 畢業前 SOL 與非 SOL 買入已接入，詳見下方路由決策。賣出、止盈止損與其他交易場所仍未接入。

簽名交易以 FULL durability 保存後才送出；逾時／查不到結果保留簽名及 mint 預留，不重簽重買。
DRY_RUN 保存 dry-simulated，不捏造已成交持倉；LIVE finalized 後以 token balance delta 入帳。
既有其他路由訂單不修改，啟動時提示人工核對；所有賣出仍需人工管理。
每段共用 `SOL_SLIPPAGE_PERCENT` 推導同一個最終輸出下限，不讓兩段滑點相乘。
第一段保留半份滑點，第二段只花保證 quote，多出 quote 留在錢包。
原生 SOL 路由以 `buy_exact_sol_in` 固定 SOL 預算並設最終 min-out；成交成本從該買入指令的實際 SOL 轉帳取值。
參考 `471cEVk3z8NvmgGRDNtTBkoaEpKnbaAA9tLHk23Kh9aHfnJrQtUDxfRLh8cBTHqprDuRyrcQsiimzr9fpQGXvyiF`
是同一筆交易中 4 個不同簽名者分別買入。解碼按指令隔離付款／收幣證據，不把 fee payer 當成全部買方；
每個地址需獨立符合 hotlist 資格。先完成該交易的全部計票／預留，再執行一次跟買，避免失敗後由後續買方重觸發。

## 發射台來源

- pump.fun 以 mint 推導 canonical bonding-curve PDA，核對 owner 和 discriminator。
- stonk 以 LaunchLab pool 的 base mint、platform ID、canonical PDA 識別，
  再檢查 platform 帳戶 owner／discriminator／fee wallet。
- 平台 `6BwHHDg3u1854jC8PDLXvR4spTcLNaoBxLJNGC4nTESt` 及
  fee wallet `5CEbueQnq1Ym2uSSx2xXds3jQAqT1BDnkA59RZobSPAG` 使用已核驗樣本，
  不以名稱或 mint 後綴判定。兩個樣本不是 mint 白名單。
- 成功證據快取最多 4096 mint／300 秒，未找到來源 15 秒；RPC 失敗不記成不存在。
- 来源核驗使用共用 `chain_common/accounts.py`，不依賴任何交易 adapter。
- `features.probe --mint` 可唯讀核驗來源；Create tx 已支援，見 [四個主網樣本與 ABI 範圍](create-tx-decoding.md)。
  支援上述畢業前 SOL／非 SOL quote Pump 買入；PumpSwap、LaunchLab 買入及賣出仍待實作。

來源規格：[Pump 官方文件](https://github.com/pump-fun/pump-public-docs/blob/main/docs/PUMP_PROGRAM_README.md)、
[LaunchLab layout](https://github.com/raydium-io/raydium-sdk-V2/blob/master/src/raydium/launchpad/layout.ts)、
[LaunchLab PDA](https://github.com/raydium-io/raydium-sdk-V2/blob/master/src/raydium/launchpad/pda.ts)。
公開帳戶證據保存在 `tests/fixtures/launchpad_origins_mainnet.json`。

## 保留的保障

收款人須 on-curve、System owner、空 data、非執行帳戶，入金金額合格。
Privacy Cash 只支持指定 native program／pool 及明確提款語義，不支援 USDC 提款。
以 signature／instruction path／recipient 去重；hotlist TTL 使用鏈上入金時間。
缺交易／時間／metadata 或 RPC 失敗不假裝成功，保留有界重試。
歷史游標在候選持久化後推進，給緊急補查及舊缺口有限處理份額。
SQLite 容量清理不刪有效證據、未完成 jobs／orders、持倉或永久成功去重。

## 驗證

離線測試覆蓋 funding、legacy／v0／v1 交易讀取、來源偽造與快取、通用策略／儲存、
Pump／Stonk SOL 與非 SOL Create、CPI、四個主網樣本、重播／最終性與拒絕錯誤資料，
資料清理、補查游標及本地 HTTP／WebSocket 服務啟動與取消。
買入測試包含指定參考交易、偽造帳戶／方向／多段歧義拒絕、達標後路由觸發、DRY_RUN 不讀私鑰／送單、
簽名持久化先於廣播、送單逾時不重買、重啟與 finalized 入帳冪等、wallet mismatch。
Node 測試使用 pinned Pump／Meteora SDK 驗證指令帳戶、滑點、lookup table、1232-byte 限制及完整組單。
`pump_route_accounts.json` 是公開帳戶快照；離線測試明確把已畢業曲線改成合成未畢業狀態，RPC 模擬回覆也是測試替身。
2026-10-05 主網唯讀 probe 確認參考 EM 曲線已畢業，正確拒絕；沒有聲稱主網買入模擬或成交成功。
原生 SOL 的 4 買方參考也已加入 fixture、解碼／隔離／計票回歸及 SDK 組單測試；唯讀 probe 時曲線亦已畢業，拒絕買入。
以當前 pytest／npm test 輸出為準；不代表實盤成交驗收。
Privacy Cash 公開 fixture 仍驗證收款人收到 29.889 SOL（pool 減少 30 SOL、fee 0.111 SOL）。


## Stonk SOL copy-buy, 2026-10-06

Reference: `39FLtNUXE65aTrBBxtTPQ8t2bPkHBoft9gqBDwnPndnVhGXGMNs3Tvy5bge5A7ddUEAn4G5gGDLM7nEUHN9NHmDM`, slot 453660847.
The top-level LaunchLab BuyExactIn spends 4.34 WSOL on mint
`J5hGf8AEr8e1yDUMoRKSvKtN5KG64WNH4G2oErAY6VVZ`. The gross token transfer is
103418416.575481; after 1% Token-2022 withholding the user receives 102384232.409726.
Only the proven quote-vault CPI counts as spending; account rent and the unrelated tip do not.

`stonk_native_curve` uses the configured SOL budget, wraps it to WSOL, then builds a fresh
LaunchLab BuyExactIn. It shares current curve/fee checks, target transfer-tax handling,
the common slippage percentage, net receipt simulation and durable execution with the
non-SOL adapter. It never calls Jupiter and bypasses its rate gate. Existing WSOL balances
and authorities are checked after simulation and retained; only a newly created WSOL ATA
is closed. Native observed-buy caps are checked directly in lamports, with no quote lookup.

The sampled pool is now migrated (status 2), and the read-only mainnet probe rejects it
with `stonk-pool-rejected`. Tests reconstruct the pre-buy reserves from its TradeEvent
for historical math and offline transaction assembly; this is not a successful live buy.
Top-level buys only; graduated pools and CPI-wrapped LaunchLab buys remain unsupported.

## Stonk non-SOL copy-buy, 2026-10-05

The reference transaction buys Stompy with DJT obtained from a Jupiter/Orca USDC swap.
The observed funding asset does not replace the configured SOL budget. After the existing
hotlist N/W threshold, a fresh SOL-to-quote Jupiter v2 build is followed by a locally
constructed LaunchLab BuyExactIn in one transaction. Only Whirlpool and Meteora DLMM
are requested/accepted for the first leg; no additional trading venue implementation is added.
No sample nonce, priority bid, relay tip, referral fee, wallet or transaction bytes are copied.
Jupiter uses keyless access with no authentication header or credential configuration.
The official documentation lists a 0.5 RPS keyless limit. A shared Python gate serializes
Stonk builders within each service event loop and spaces completion-to-start by two seconds,
so fresh Node subprocesses cannot bypass the limit. Separate processes and other traffic
sharing the IP are outside this local gate. A 429 starts a Retry-After cooldown (60 seconds
if unavailable); long cooldowns reject fresh builds rather than queue stale signals.
Pump builders bypass this gate. Signed transactions are sent only through the configured public RPC.

Only the proven Stonk platform, derived pool/vaults, current constant-curve state and current
protocol/platform/creator rates are used. Target Token-2022 transfer fees are subtracted before
applying the end-to-end slippage bound; LaunchLab receives a net minimum, as in its official SDK.
Quote-token transfer fees remain unsupported. A bounded first-hop minimum funds the second leg;
excess quote stays in the wallet. Simulation checks net target gain and preservation of existing
quote/intermediate token balances and authorities. The existing durable sign/send/reconcile path
remains responsible for deduplication and finalized fills.

Sources: https://developers.jup.ag/docs/llms.txt ; https://developers.jup.ag/docs/swap/build ;
https://github.com/raydium-io/raydium-sdk-V2/tree/master/src/raydium/launchpad ;
https://github.com/raydium-io/raydium-idl/blob/master/raydium_launchpad/raydium_launchpad.json .
The actual sample and read-only account snapshot are fixtures. Its pool is now graduated;
tests reconstruct historical pre-trade reserves and mock first-hop routing/simulation explicitly.
No live order or mainnet atomic simulation has been claimed from those offline tests.


## 2026-10-07: first-observation USD market-cap admission

`SOL_FOLLOW_MAX_MARKET_CAP_USD_K=20` means USD 20,000. Zero disables
checks for new mints; it does not clear existing exclusions. After changing the
setting, restart the service. Existing trading, slippage and fee guards remain.

The first fresh, eligible hotlist buy obtains a pool snapshot before counting
wallet votes, including when N is one. FDV is the pool spot price multiplied by
the mint's current total supply, following the price-times-supply approach in
RH's entry valuation. Pump uses virtual quote/base reserves; Stonk LaunchLab
uses (virtual quote + real quote)/(virtual base - real base). Non-SOL quotes use
the existing validated SOL-to-quote Whirlpool/DLMM route in reverse for valuation.
This is a spot valuation, not proceeds from selling the entire supply.

SOL/USD comes from the Pyth on-chain SOL/USD push feed. Its owner, feed ID,
full verification, publication age (at most 90 seconds), and confidence (at most
1% of price) are checked. The new and legacy receiver deployments are accepted.
The existing public RPC/WebSocket account cache is used, with a cold public RPC
batch when necessary. No extra API key or periodic HTTP price refresh is added.
The snapshot must be at or after the triggering slot; it may be later than the
triggering transaction and is not a historical reconstruction of that exact
transaction. The first observation can therefore incur a public RPC wait.

The first result is saved in `trading.sqlite3`'s `token_entry_caps` table with its
trigger signature/slot and valuation details. Above the cap is permanently
blocked; exactly equal is allowed. Subsequent wallets reuse that first result,
including across restarts. Lowering the limit also checks the saved first cap;
raising/disabling it never releases an already blocked mint. Missing/invalid
price, supply, route or an interrupted first check stays closed until manual
reset: a later buyer must not silently supply a different first price. Blocked
or unavailable results notify Telegram once when recorded. These rows do not
expire with hotlist entries or rolling wallet votes.

Inspect without sending transactions:

```sh
python -m features.strategy.market_cap list
python -m features.strategy.market_cap list --mint MINT_ADDRESS
```

Stop the service before manually clearing one mint, then restart it:

```sh
python -m features.strategy.market_cap reset --mint MINT_ADDRESS
```

Reset also clears that mint's pending votes; the next fresh eligible buy gets a
new valuation. It does not replay previously consumed events or remove the
existing order deduplication. Reset refuses a mint with an active order.

## 2026-10-07: qualification backlog notification

The existing 60-second health check reports one incident when the pending
qualification/transaction queue grows for three consecutive checks, or its
oldest item exceeds five minutes. The alert latch is persisted across restarts.
Three healthy checks (queue not growing and oldest below one minute) rearm it;
recovery is recorded locally without an extra Telegram message. Undecoded
funding candidates are included so a bottleneck before qualification is visible.
