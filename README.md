# sol-follow

獨立 Solana 服務：CEX／Privacy Cash 原生 SOL 入金 → 資格檢查 → SQLite hotlist，
並解碼監聽交易中的 Pump／Stonk Create。
群體跟買已接入 Pump 及 Stonk 原子買入路由；賣出與分批止盈／止損仍待實作。
**沒有修改、import 或共用 RH/BSC 的 runtime、錢包和資料庫。**

發射台範圍固定為 **pump.fun 和 stonk 旗下代幣**。以下地址只作來源驗證樣本，不是 mint 白名單：

- pump.fun：`CBLx6CRcCTtbmgTdxpqnF2dP1MpWbMUjngtNbFTApump`
- stonk：`49tVXDe7c44LGg95brseq1KfzFr4SvoYwDMrm1Xn7zC6`

發射台來源使用鏈上帳戶證據驗證；名稱、`pump` 後綴或交易場所都不能作證。
`launchpads/` 負責來源識別，買入路由放在 `trade_execution/`。
stonk 目前識別樣本所屬的 LaunchLab platform；未驗證的其他 platform／舊版發射路徑不自動納入。

**Pump 畢業前非 SOL quote 買入已改為通用路由：SOL → 自動尋找／快取的 quote 換幣路徑 → Pump。**
quote mint 沒有 SPCX／DJT／USDC 白名單；按 Pump 指令、mint program 及曲線狀態識別。
目標可直接花既有 quote，或經其他平台換幣後買 Pump；不再要求前一段必須是 DLMM。
支援 `buy_v2`／`buy_exact_quote_in_v2` 頂層及具有完整 stackHeight／轉帳證據的 CPI；
同錢包同 mint 的多筆買入無法唯一歸屬時拒絕計票。Create 階段便保存 quote、預熱及尋路。
自己的 SOL → quote 路徑共用 Stonk 的 Orca Whirlpool／Meteora DLMM 引擎，最多三跳，按 quote mint 永久保存路徑。
原有直接 DLMM 樣本可提供路徑提示，舊路由仍相容；實際建單重新驗證池 owner、mint、流動性及最新報價。
**通用 mint 不代表任意 DEX 或任意 Token-2022 擴充都可執行**；沒有可用路徑、未知 hook、
轉帳稅、不足流動性、費用／滑點超標或超過交易大小上限時拒絕。詳見 [Pump 非 SOL 路由](docs/pump-quote-routing.md)。
亦支援 **畢業前原生 SOL → Pump** 買入，辨認 `buy`／`buy_exact_sol_in`。
同一交易的多個買方按各自指令、簽名者及實際 SOL／token 轉帳分開解碼；每個合資格 hotlist 地址一票。
仍只使用設定錢包跟買一次，不複製舊交易的數量或指令。
服務仍保留 CEX／Privacy Cash 入金資格、hotlist、交易取得／補查、持久化佇列及健康通知。
pump.fun／stonk 鏈上來源核驗可透過 `--mint` probe 使用。
**Pump／Stonk 的 SOL 與非 SOL 對 Create tx 解碼已接入**，支援頂層／可驗證 CPI、
legacy／v0／v1，保存 mint、creator、quote、curve／pool 及名稱等欄位。
四個指定主網樣本已加入回歸 fixture；詳見 [Create 解碼範圍](docs/create-tx-decoding.md)。
新增 Stonk 非 SOL 報價幣的頂層 `BuyExactIn` 識別，包含前段透過 Jupiter 換幣的樣本。
自己的跟買仍用 `SOL_BUY_AMOUNT_SOL`：Jupiter `/swap/v2/build` 尋找 SOL → quote（Orca Whirlpool／Meteora DLMM），
再接 LaunchLab 買入，合成一筆本地組裝的交易，由公共 RPC 模擬及送出。
Jupiter 使用免 key 存取（0.5 RPS），不需認證 header；不使用 Jupiter 代送、不加樣本小費或 integrator fee。
同一程序的 Stonk 組單共用限速，上一筆完成後至少隔 2 秒；收到 429 按 Retry-After 冷卻（缺省 60 秒）。
長冷卻期間拒絕新組單，避免等待過期訊號；多程序或共用 IP 的其他流量仍可能觸發供應商限流。
支援已核驗 Stonk platform 的未畢業 constant curve，目標幣可有 Token-2022 轉帳稅；
報價讀當前 epoch 費率，滑點下限及持倉均按扣稅後實收。報價幣有轉帳稅、未知平台、畢業池、
CPI 內的 LaunchLab 買入、缺少有效尋路或交易超過 1232 bytes 時拒絕。
Stonk SOL 報價買入、PumpSwap、其他 quote 換幣場所、其他 CPI 聚合路由及所有賣出仍待實作。
新買單保存簽名後才送出，依 finalized 交易核對實收數量；未知送單結果保留預留，不另簽新買單。
舊訂單／持倉資料保留，通知人工管理；目前沒有自動賣出或止盈止損。

## 啟動 DRY_RUN

Python 3.11+、Node 20.18+；在 **sol-follow 目錄** 執行，不能在上層 token_alert 執行同名 package。

```bash
cd /home/ubuntu/sol-follow
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm ci --ignore-scripts
cp .env.example .env  # 僅全新部署；不要覆蓋已有 .env
.venv/bin/python -m features.app check
.venv/bin/python -m features.app run
```

Windows PowerShell 使用 `.\.venv\Scripts\python`，例如：

```powershell
cd C:\Users\kenho\Documents\learning\sol-follow
.\.venv\Scripts\python -m features.app check
.\.venv\Scripts\python -m features.app run
```

目前本地 `.env` 是 `DRY_RUN=true`、N=3、窗口 120 秒、買額 0.01 SOL；這些是可調初值。
正式運行請填公共 `SOL_RPC_HTTP_URL`、`SOL_RPC_WS_URL`；報價、模擬、買入送單及 HTTP 查詢使用此公共 RPC。
在本專案 `.env` 加入 `ALCHEMY_API_KEY` 並設定 `SOL_FEED_MODE=alchemy_grpc`，
只有 hotlist 交易改用 Alchemy 完整交易串流；不會切換 HTTP 到 Alchemy。
正常運作不循環查詢所有地址歷史，只在入列、首次訂閱或斷線補漏時觸發有界補查。
設定、重播範圍及一萬地址連線測試見 [Alchemy hotlist 訂閱](docs/alchemy-hotlist-stream.md)。
公共 RPC 僅適合短暫探測，不能承諾訂閱上限／延遲／零漏單。環境變數優先於 `.env`。
Telegram 填本專案 `.env` 的 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`，不自動讀上層憑證。

`SOL_HOTLIST_TTL_HOUR=24` 表示 hotlist 有效 24 小時；`SOL_AUDIT_RETENTION_HOUR=72`
表示審計保留 72 小時。兩者支援小數小時，程式會換算成秒。
`SOL_SLIPPAGE_PERCENT=2` 表示整條路徑的最終最低收幣量至少為即時預估輸出的 98%。
第一段使用半份滑點保留 quote，Pump 只花該保證數量；多換到的 quote 留在錢包，不會為湊單花用既有 quote。
`SOL_MIN_POOL_LIQUIDITY_SOL` 檢查 DLMM 的 WSOL reserve；報價亦要求能完成全額交換。
原生 SOL 路由直接按 bonding curve 與當前費率報價，以 `buy_exact_sol_in` 花設定的 SOL 預算，
`min_tokens_out` 使用同一個滑點百分比。DLMM reserve 門檻不套用到這條原生 SOL 路由。

DRY_RUN 在達標時組未簽名交易及模擬，不讀私鑰、不送單，不建立虛假的已成交持倉。
未設定公開錢包時，DRY_RUN 使用訊號錢包作模擬付款人；可設定 `SOL_WALLET_ADDRESS` 模擬自己的資金狀態。
LIVE 需要 `SOL_WALLET_KEYPAIR_PATH`（Solana CLI JSON keypair）及相符的 `SOL_WALLET_ADDRESS`，
模擬通過且訊號未過期後才簽名、保存、送單。買額不包含交易費與新 ATA 租金。

## 買入路由範圍

Pump 支援上述 DLMM → Pump 和原生 SOL → Pump 路徑；曲線已畢業、池的 quote 不符、活躍 transfer hook、轉帳費、
暫停／預設凍結帳戶、流動性不足、交易超過 1232 bytes、模擬失敗均拒絕。
支援普通 SPL 和可公開轉帳的 Token-2022（包含 SPCX 的未啟用 hook、未暫停擴充）。
使用訊號交易的 lookup table 作壓縮提示，帳戶及數量由 SDK 重新推導。
CU 上限按完整交易模擬消耗自動加 20% 餘量；沒有另設優先費參數。
`check` 會列出 `meteora_dlmm_to_pump_curve`、`pump_native_curve` 及 `sol_to_stonk_curve`。
`check` 的 `jupiter_access=keyless` 表示免 key 模式，不代表 API、流動性或 LIVE 實測已通過。
DRY_RUN 的 `trading_enabled=false`。Stonk 本地路徑亦檢查第一段池的 WSOL reserve，並套用相同流動性門檻。

Stonk 回歸樣本：`2R6jsVXbZN2CRN59VwHysxgadAgropybVwk7ou2DQYjDLJ74CFqgf7cmMhCkYNZ5cDJk9Ki3Un17mxjBwKXffaDT`。
歷史交易的 JSON／protobuf 解碼和扣稅報價有離線測試。樣本池目前已畢業；
離線組單測試使用明確重建的歷史池狀態及 mocked Jupiter／模擬回應，不代表主網完整買入成功。
首次尋路需要 Jupiter 免 key 服務；已保存的路徑用本地 SDK 重算。完整主網模擬仍需要當前未畢業池。

## 非 SOL 代幣快取及報價限制

服務在解碼到受支援的非 SOL Create／買入時就保存目標 mint、quote mint、token program、池及路徑，
不必等 hotlist 票數達標。`funding.sqlite3` 的 `seen_non_sol`、`seen_quote_mints`、`quote_recipes`
不按 TTL 清除；啟動時亦匯入仍保留在資料庫的歷史 Create。它們是身分／路徑提示，不是價格、資格證據或已簽交易。

常駐 Node 程序保留 SDK 及最多 `SOL_QUOTE_CACHE_ACCOUNTS=512` 個活躍帳戶。
帳戶及 ALT 使用 `SOL_RPC_WS_URL` 的公共 WebSocket 更新，不再定時 HTTP 刷新帳戶。
首次讀取、斷線恢復、快取淘汰或資料 slot 落後訊號時，才按需 HTTP 補查。
`SOL_QUOTE_CACHE_TTL_MS=2000` 限制未取得連續訂閱覆蓋的快照及 WebSocket 心跳時效；
訂閱已確認、初始快照完整且連線健康時，未變動帳戶可以繼續使用。心跳不會提升帳戶資料 slot，
池資料仍須不早於訊號交易。斷線或 processed 分叉失效會清除快取，補查失敗則拒絕建單。
ALT 同樣訂閱更新；停用的表拒絕使用，新增地址須遵守下一 slot 才可用的限制。

`getLatestBlockhash` 使用共用背景快取，預設 `SOL_BLOCKHASH_REFRESH_MS=1000`、
`SOL_BLOCKHASH_CACHE_TTL_MS=5000`；同一建單的兩次模擬共用同一 hash。
買入不因 hash 缺失而即時查 RPC；快取過期、距觀察到的 slot 超過 32 或失效便拒絕該次建單。
背景 blockhash HTTP 更新仍保留；取消的是帳戶的定時 HTTP 輪詢。

交易期間，歷史補查、預熱、Jupiter 背景尋路、訂單對帳及 blockhash 更新會暫停發起新請求。
已送出的請求可以完成，WebSocket 更新繼續接收。Node 建單完成後，退避持續至 Python 模擬／送單結束。
背景預熱佇列最多 128 個，重啟預熱最近 32 個；舊代幣身分／路徑永久保留，冷門代幣可能需要重新補齊帳戶。
沒有把所有曾見過的地址永久訂閱，也沒有改用 Alchemy HTTP。
health 的 `quote_cache` 顯示帳戶及 blockhash hits／misses。

快取完整時，建單及送單路徑省去原本兩次即時 blockhash 查詢；仍保留 SDK 模擬、Python 模擬及
`sendTransaction` preflight。首次代幣或 slot 落後仍可能增加帳戶讀取。
這能縮短等待，但公共 RPC 延遲、processed 訊號到達時間及 leader 排程仍影響成交 slot；沒有同 slot 保證。

Stonk 首次以 Jupiter `maxAccounts=32` 尋找 Whirlpool／Meteora DLMM 路徑，保存池與 lookup table 地址。
其後依最新池資料使用 Orca／Meteora SDK 本地報價及組指令，快取命中不必再等 Jupiter。
只接受單一路徑、最多三段；拒絕拆單、循環及未知場地。每段只花上一段的保證輸出，多出的中間代幣留在錢包。
Pump 非 SOL Create／買入共用 quote 路徑快取並預熱換幣帳戶；已知 DLMM 前段可作路徑提示，其他 quote 自動尋路。

預設限制（皆為百分比，可在 `.env` 覆蓋，精度 0.01%，0 表示不容許）：

- `SOL_MAX_POOL_FEE_PERCENT=2`：任何單池超過 2% 拒絕。
- `SOL_MAX_TOTAL_FEE_PERCENT=3`：整條路徑池費／Stonk 轉帳稅合計超過 3% 拒絕。
- `SOL_MAX_PRICE_IMPACT_PERCENT=2`：扣除費用後，相對各池當下邊際價格的合計價格影響超過 2% 拒絕。
- `SOL_SLIPPAGE_PERCENT=2`：保留原有最終實收 minOut，至少為完整即時預估淨輸出的 98%。

前三項是建單前的成本上限，與鏈上的 minOut 各自生效；不把高費用藏在滑點內。
Stonk 依 [官方 LaunchLab SDK](https://github.com/raydium-io/raydium-sdk-V2/blob/master/src/raydium/launchpad/launchpad.ts#L628-L695)
以扣除轉帳稅的數量計算 minOut，模擬亦核對淨收幣及既有中間資產未被花用。
價格影響基準是當下池價，不是獨立公允價格；minOut 限制可接受的惡化，不能保證完全免受夾單。
交易區塊雜湊、必要 lookup table 驗證、模擬及送單仍需 RPC；曾見過不代表永遠零 RPC。

`tests/fixtures/local_quote_accounts.json` 及 `local_dlmm_quote_accounts.json` 是 2026-10-06 公開 RPC 快照。
離線測試以實際 Orca／DLMM SDK 報價，接上重建的歷史 Stonk 曲線，驗證完整封包、各段 minOut 與成本限制。
模擬回應為測試替身，並不表示曾在主網完整買入。

## 指令

```bash
.venv/bin/python -m features.app stats
.venv/bin/python -m features.probe --mint CBLx6CRcCTtbmgTdxpqnF2dP1MpWbMUjngtNbFTApump
.venv/bin/python -m features.probe --mint 49tVXDe7c44LGg95brseq1KfzFr4SvoYwDMrm1Xn7zC6
.venv/bin/python -m features.probe --samples 3
.venv/bin/python -m features.probe --tx SOLANA_SIGNATURE
.venv/bin/python -m features.probe --tx SOLANA_SIGNATURE --buy-route
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
npm test
```

probe 不開資料庫、不讀私鑰，最多抽 10 筆；`--tx` 列出交易版本、入金與 Create 解碼，
`--raw` 可列出成功解析的公開原始交易。`--mint` 只驗來源。
`--buy-route` 只重新報價及模擬未簽名原子買入，不讀私鑰、不送單、不寫資料庫。
歷史簽名只作路徑提示；若代幣現已畢業，會拒絕。不要用 probe 當高頻監控。

## systemd

把整個資料夾獨立部署到 `/home/ubuntu/sol-follow`，安裝依賴、設定 `.env`，先 `mkdir -p data`。
檢查並按實際路徑修改 `deploy/sol-follow.service` 的 User／WorkingDirectory／ExecStart／ReadWritePaths，然後：

```bash
sudo cp deploy/sol-follow.service /etc/systemd/system/sol-follow.service
sudo systemctl daemon-reload
sudo systemctl enable --now sol-follow
sudo journalctl -u sol-follow -f
```

舊訂單與持倉保留在資料庫。新路由的已簽名訂單會恢復成交核對；未簽名的中斷訂單取消。
舊路由訂單及所有持倉退出仍需人工處理。

## 容量與保障邊界

`data/funding.sqlite3`、`trading.sqlite3`、`audit.sqlite3` 分庫；總 DB/WAL/SHM 預算預設 2 GiB，
90% 提前清理／通知，目標 1.5 GiB。增量回收空頁，不在交易期做完整 VACUUM。
這是保護有效證據的**軟上限**，不是無條件刪資料的硬限額：若容量全是有效 hotlist／未完成任務／持倉／
去重證據，就告警而不刪除它們；真正磁碟滿會停止無法持久化的交易，不能保證繼續買。
審計容量優先可淘汰，不代表完整歷史計數。一般資料採用 WAL + NORMAL；新買單簽名在廣播前以 FULL durability 保存。

策略／取捨和測試範圍見 [docs/DECISIONS.md](docs/DECISIONS.md)，原設計見 [IMPLEMENTATION.md](IMPLEMENTATION.md)。


## Processed hotlist 訊號與買額上限

`SOL_HOTLIST_COMMITMENT=processed` 使用 Alchemy gRPC 完整交易及執行 metadata，
直接解碼 hotlist 買入，沒有 `getTransaction`／`getBlockTime` 等待。
有 Alchemy key 時預設 processed；legacy websocket 只能用 confirmed。
CEX 入金資格仍使用 confirmed 證據。processed 是節點已執行、尚未確認，並非執行前 pending。

`SOL_FOLLOW_MAX_TARGET_BUY_SOL=5` 限制目標錢包每次買入的 calldata 付款預算，
等於 5 SOL 可跟買、大於便跳過；本人的買額仍由 `SOL_BUY_AMOUNT_SOL` 控制。
Pump `buy`／`buy_v2` 使用最高付款欄位；exact-input 使用輸入預算，
因此即使實際成交低於 5 SOL，只要指令允許支付更多也會保守跳過。
SOL 直接比較 lamports；非 SOL 用已記錄路徑、有效期內的本地池快照計算
5 SOL 可換得多少付款代幣，再與目標輸入額比較，包含該兌換的費用及價格影響。
這是近期快取的 SOL 重置成本估算，非逐筆還原目標錢包先前換幣成本；
快照可早於目標交易（預設最多 2 秒），本人建單仍保留 minContextSlot。
DLMM→Pump 亦檢查第一段 SOL 預算，避免忽略既有付款代幣或超額第一段。

上限檢查只讀取 calldata、本地 SQLite 路徑和 Node 記憶體快照，不發報價 RPC／Jupiter 請求。
沒有路徑、缺少快照、過期或風險限制未通過時跳過該訊號，並由背景佇列預熱；
不會等補齊後追買同一事件。帳戶／epoch 背景更新與本人建單、模擬及送單仍使用公共 RPC。
既有 minOut、滑點、池費、總費用和價格影響限制保留。

重播訊號必須通過即時 slot／區塊時間錨點檢查，不能把收到歷史交易的時間當成新交易。
缺少 blockTime 的即時訊號使用明確標記的首次接收時間；確認後另走公共 RPC 補資格及 Create 記錄。
processed 來源和每張訂單的來源票數均持久化；dead slot、來源失敗、slot 改變或超過 30 秒未能確認會撤票，
建單／簽名／送出前再次檢查。背景確認使用批次 getSignatureStatuses，並不輪詢 hotlist 地址的交易歷史。
同一事件在 processed／confirmed 間不會重複跟買。已廣播的跟買無法因來源回滾而撤回。


Processed 模式只容許即時 processed 來源新增跟買票數及觸發買單。
confirmed 補查仍可更新入金資格、Create、持倉及快取，但不能觸發補買；舊 confirmed-only 票數不計入 processed 策略。
若背景已得知觸發交易 confirmed/finalized，尚未送出的跟買也會停止。這只依據本地已收到的狀態，不能保證鏈上尚未確認。
processed 不保證同 slot 成交；`minContextSlot` 是 RPC 最低讀取 slot，不是交易有效 slot 上限。
建單、模擬、公共 RPC 排隊及區塊收錄仍有延遲。本程式沒有鏈上同-slot 限制指令。
部署時明確設定 `SOL_FEED_MODE=alchemy_grpc`、`SOL_HOTLIST_COMMITMENT=processed`，
並配置 `ALCHEMY_API_KEY`，可讓缺少 key 時直接報配置錯誤，避免 auto 模式選用 confirmed websocket。

## 非 SOL 跟買失敗通知

設定 `TELEGRAM_BOT_TOKEN` 及 `TELEGRAM_CHAT_ID` 後，非 SOL 的目標買額檢查失敗
（包括快取缺失、超過上限）及合資格 hotlist 錢包的已知 Pump／Stonk 非 SOL 買入指令
未通過路由驗證，都會排入 Telegram 通知。
建單／尋路／池費檢查、模擬、簽名、送單及鏈上失敗也會通知；重啟時中斷的未簽訂單會通知。
內容包含代幣、報價幣、階段、可安全顯示的原因、來源交易及可取得的自身交易簽名。
送單逾時仍標示「結果未知」並保留訂單，不當作確定失敗而重新買入。
DRY_RUN 的失敗同樣通知並標示 dry。背景預熱本身、非 hotlist 地址及未達策略門檻不當成買單失敗。
同一事件重試的跳過通知會去重，不使用所有代幣共用的限頻 key。
通知沿用獨立非阻塞佇列；Telegram 未設定、服務不可用或佇列滿時仍可能送達失敗，請查看服務日誌。
