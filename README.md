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

**目前支援 Pump 畢業前非 SOL quote 的原子買入：SOL → WSOL → Meteora DLMM → quote token → Pump。**
亦支援 **畢業前原生 SOL → Pump** 買入，辨認 `buy`／`buy_exact_sol_in`。
同一交易的多個買方按各自指令、簽名者及實際 SOL／token 轉帳分開解碼；每個合資格 hotlist 地址一票。
參考 `471cEVk3…QGXvyiF` 中的 4 個買方不會變成機器人的 4 個下單錢包；仍只使用設定錢包跟買一次。
辨認監聽交易中同一簽名者的頂層 DLMM `swap2` 及 Pump `buy_v2`／`buy_exact_quote_in_v2`，
hotlist 錢包達到 N/W 門檻後，用當前鏈上狀態重新報價、組單；不複製舊交易的數量或指令。
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

常駐 Node 程序保留 SDK 及最多 `SOL_QUOTE_CACHE_ACCOUNTS=512` 個活躍帳戶，
公共 RPC account 訂閱配合每秒最多一批 100 帳戶刷新。每次讀取必須在 `SOL_QUOTE_CACHE_TTL_MS=2000`
內且 context slot 不早於訊號交易；過期、slot 落後、斷線時重新讀取，讀取失敗便拒絕建單。
背景預熱佇列最多 128 個，重啟預熱最近 32 個；舊代幣的資料仍永久保留，但冷門代幣再次出現可能需要刷新。
沒有把所有曾見過的地址永久訂閱，也沒有改用 Alchemy HTTP。health 的 `quote_cache` 顯示 hits／misses／accounts。

Stonk 首次以 Jupiter `maxAccounts=32` 尋找 Whirlpool／Meteora DLMM 路徑，保存池與 lookup table 地址。
其後依最新池資料使用 Orca／Meteora SDK 本地報價及組指令，快取命中不必再等 Jupiter。
只接受單一路徑、最多三段；拒絕拆單、循環及未知場地。每段只花上一段的保證輸出，多出的中間代幣留在錢包。
Pump 的已知 DLMM → Pump 路徑同樣使用帳戶快取；只有 Create、尚未知道 DLMM 池時先預熱 mint／曲線。

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
