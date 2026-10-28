# sol-follow

入金來源 WebSocket：有 Alchemy key 且 `SOL_RPC_WS_URL` 為 Solana 公共預設時，使用 Alchemy 的 `wss://solana-mainnet.streaming.alchemy.com/v2/<key>`；自訂 WebSocket 地址保留，也可用 `SOL_FUNDING_WS_URL` 指定。斷線時輪替嘗試原有 `SOL_RPC_WS_URL`。啟動／health 的 `funding_ws_provider` 顯示首選來源，不輸出 key；個別連線日誌的 `provider` 顯示該次實際來源。
Hotlist gRPC 啟動的 `getSlot` 使用公共 RPC，失敗後嘗試 Alchemy；`stream_head_rpc` 顯示後備統計。連線錯誤附帶 `stage` 或 `reason`／數字錯誤代碼，方便區分 head 查詢、訂閱拒絕及遠端關閉。
RPC 單筆回傳 `null`、驗證不合格或要求的 slot 尚未可用，只讓該工作走後備／重試，不會暫停供應商其他查詢。429、網路／服務故障仍保留冷卻。health 另列 `*_result_unavailable`、`*_errors`、`*_cooldown_skips`、`last_failure`；本地搜尋 `RPC read deferred` 可查看不含 key 的原因。`unavailable` 是失敗呼叫次數，包含同一工作的重試，並非失敗交易數。
入金 RPC 的雙供應商失敗先只記日誌；同一方法持續失敗超過 60 秒，在下一次失敗時才發 Telegram，每輪故障通知一次。任一供應商成功取得有效結果即重設該方法的計時；短暫失敗及恢復不發 Telegram。

買賣的 Python RPC、Node 建單 HTTP、交易對帳均支援公共 RPC → Alchemy 後備。入金、交易發送、交易對帳及訊號確認各有獨立連線池／工作佇列；同一端點的真實 429 共用冷卻，所有通道會避開該端點並嘗試後備。冷卻以雜湊端點名稱保存於 data/rpc-cooldowns，Python 與短期／常駐 Node 建單程序均讀取；單筆 null、錯誤參數或未索引不會寫入共用冷卻。預設沿用現有公共 RPC 和 Alchemy key，毋須新增供應商。**這是程式隔離，不是供應商配額隔離**：相同 IP／Alchemy 帳戶仍可能共用上限。
發送後備只重送已寫入資料庫的相同簽名及 bytes，不重簽；未知結果保留，RPC 接受不等於已成交。模擬的有效鏈上錯誤不會被後備的成功結果蓋過，兩端均保留 commitment、minContextSlot 與 preflight。每個交易端點使用前會核對 mainnet genesis。
每次買入前先同步登記 dev 與其 token account 的監察，刷新實際 MarketFeed，最多等待 2 秒取得訂閱確認；未接好就停止該筆買入。訂閱確認仍會觸發 dev sell 空窗補查。
止盈及 dev 賣出使用獨立的公共／Alchemy WebSocket 同時監聽，事件由原有持久化鍵去重；不再與 CEX 入金共用訂閱。任一連線維持完整覆蓋時不發斷線 Telegram；覆蓋不足超過 60 秒才通知。入金／gRPC 斷線與確認／交易 RPC 故障亦先記本地，持續超過 60 秒才告警；真正的交易拒絕、鏈上失敗等交易通知照常。
gRPC 模式另有常駐 confirmed create 備援 WebSocket，優先公共端點、斷線可輪替原有 funding 端點。只訂閱現有 hotlist，create log 須再以 confirmed getTransaction 核對簽名、slot、成功狀態、簽署 dev 及 create 指令，再走相同 hotlist、aged-funding、dev 持倉、風控與去重。備援也適用 processed 模式，但不偽造 processed 證據。兩個 feed 同時送達只會保留一筆買入；通知及鏈上時間都須在 signal age 內，重啟／歷史補查不會追買。confirmed 備援比 processed 串流慢，且所有來源同時不可用時仍可能漏訊號。入金補查使用下述固定時間窗口與持久續查游標，每輪 3 頁不再是整個缺口的上限。
可選設定：`SOL_EXECUTION_RPC_HTTP_URL`、`SOL_EXECUTION_RPC_FALLBACK_HTTP_URL`、`SOL_EXECUTION_RPC_WS_URL` 可接入另有配額的交易端點；`SOL_EXECUTION_RPC_TIMEOUT_MS=4000` 是每個 HTTP 請求的時限。HTTP 設定空白且有 Alchemy key 時，組單讀取、模擬及交易送出優先使用 Alchemy，`SOL_RPC_HTTP_URL` 作後備；沒有 key 才直接使用原有 RPC。明確設定的端點仍優先；WS 設定不變。`features.app check` 顯示 `execution_rpc_fallback`、`execution_shares_funding_endpoint`、`market_feed_connections`，不輸出端點或 key。

畢業代幣的 dev 入場／create 買入理論研究：`python graduation_watch.py watch`，持續收集至手動停止。
使用獨立研究資料庫，不依賴真實成交；啟動及報告定義見 [畢業研究監控](docs/graduation-research.md)。

獨立 Solana 服務：CEX SOL／USDC、Privacy Cash 原生 SOL 入金 → 資格檢查 → SQLite hotlist，
並解碼監聽交易中的 Pump／Stonk Create。
買入模式已改為 **hotlist dev create 狙擊**：合資格、實際簽名的 dev 建立 Pump／Stonk 代幣便觸發一次買入，無須多錢包買入票數，也無須 create 交易附帶 dev buy。
Create 先消耗 hotlist，再檢查入金年齡、dev 持倉與原子買入風控；市值條件已取消。重播、補查及重啟不追買。詳見 [Create 狙擊模式](docs/dev-create-sniping.md)。
`SOL_BUY_ONLY_AGED_MINUTES=10` 代表最新合資格入金至少 10 分鐘才可狙擊；`0` 關閉。未達門檻的 create 消耗資格後跳過，不作延遲補買。
Dry run 的「偵測到 create」Telegram 通知也套用此門檻：未達入金年齡、沒有合資格入金或入金時間不明時不通知；設為 `0` 則維持原有通知行為。
支援 dev 超標緊急賣出，一般分批止盈／止損仍待實作。
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
同一交易的多個買方仍可解碼及提供換幣路徑提示；一般買入交易不再計票或觸發狙擊。
仍只使用設定錢包跟買一次，不複製舊交易的數量或指令。
服務仍保留 CEX／Privacy Cash 入金資格、hotlist、交易取得／補查、持久化佇列及健康通知。
pump.fun／stonk 鏈上來源核驗可透過 `--mint` probe 使用。
**Pump／Stonk 的 SOL 與非 SOL 對 Create tx 解碼已接入**，支援頂層／可驗證 CPI、
legacy／v0／v1，保存 mint、creator、quote、curve／pool 及名稱等欄位。
四個指定主網樣本已加入回歸 fixture；詳見 [Create 解碼範圍](docs/create-tx-decoding.md)。
支援 Stonk SOL 與非 SOL 報價幣的頂層 `BuyExactIn` 識別，包含前段透過 Jupiter 換幣的樣本。
SOL 對使用 `stonk_native_curve`：本地包裝 WSOL 後直接呼叫 LaunchLab，無需 Jupiter 報價或限速等待。
金額沿用 `SOL_BUY_AMOUNT_SOL`，扣稅後 min-out 使用共用 `SOL_SLIPPAGE_PERCENT`；既有 WSOL 餘額保留。
非 SOL 對的跟買亦用 `SOL_BUY_AMOUNT_SOL`：Jupiter `/swap/v2/build` 尋找 SOL → quote（Orca Whirlpool／Meteora DLMM），
再接 LaunchLab 買入，合成一筆本地組裝的交易，由公共 RPC 模擬及送出。
Jupiter 使用免 key 存取（0.5 RPS），不需認證 header；不使用 Jupiter 代送、不加樣本小費或 integrator fee。
需要 Jupiter 的非 SOL 組單共用限速，上一筆完成後至少隔 2 秒；收到 429 按 Retry-After 冷卻（缺省 60 秒）。
長冷卻期間拒絕新組單，避免等待過期訊號；多程序或共用 IP 的其他流量仍可能觸發供應商限流。
支援已核驗 Stonk platform 的未畢業 constant curve，目標幣可有 Token-2022 轉帳稅；
報價讀當前 epoch 費率，滑點下限及持倉均按扣稅後實收。報價幣有轉帳稅、未知平台、畢業池、
CPI 內的 LaunchLab 買入、缺少有效尋路或交易超過 1232 bytes 時拒絕。
PumpSwap 買入、其他 quote 換幣場所、其他 CPI 聚合買入及一般止盈／止損賣出仍待實作。
新買單保存簽名後才送出，依 finalized 交易核對實收數量；未知送單結果保留預留，不另簽新買單。
舊訂單／持倉資料保留；自動賣出目前僅處理有完整跟買紀錄的 dev 超標退出，詳見下節。

## 啟動 DRY_RUN

Python 3.11／3.12（建議 3.12）、Node.js 22 LTS；在 **sol-follow 目錄** 執行，不能在上層 token_alert 執行同名 package。
目前固定的 aiohttp／grpcio 版本沒有 Python 3.14 對應 wheel，會退回原始碼編譯；部署不要使用 3.14。

```bash
cd /home/ubuntu/sol-follow
python3.12 -m venv .venv
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
`SOL_RPC_HTTP_URL`、`SOL_RPC_WS_URL` 供背景處理使用；有 Alchemy key 時，交易 HTTP 預設 Alchemy 優先、原有 RPC 後備。背景公共 RPC 仍可能回報 429；Alchemy 亦受帳戶配額限制。
在本專案 `.env` 加入 `ALCHEMY_API_KEY` 並設定 `SOL_FEED_MODE=alchemy_grpc`，
hotlist 交易使用 Alchemy 完整交易串流。只要有此 key，CEX／Privacy Cash 來源地址的
`getSignaturesForAddress` 補查亦使用 Alchemy HTTP（即使 feed 是 websocket）。
沿用 `SOL_ALCHEMY_API_KEY` 別名；沒有 key 時來源補查使用原有公共 RPC。
Alchemy 來源補查失敗會嘗試公共 RPC，兩邊失敗則保留任務重試。入金交易詳情及收款錢包資格檢查先用公共 RPC，失敗時嘗試 Alchemy。
有 Alchemy 後備的入金查詢，單一供應商的 429 只保留日誌／審計；後備成功不發 Telegram。
同一方法兩邊都未取得有效結果持續超過 60 秒，才發「入金 RPC 查詢失敗」，每輪故障一次。
finalization／processed 訊號的 `getSignatureStatuses` 確認亦先用公共 RPC，失敗時用 Alchemy 後備。
後備成功不發 Telegram；兩邊持續失敗超過 60 秒才發「交易確認查詢失敗」，每輪故障一次，待確認資料保留重試。
合法的 `null` 狀態仍代表未確認；鏈上失敗、slot 不符及原有撤票規則照常執行。
`SOL_BACKGROUND_RPC_RPS` 預設為 2（公共 RPC）；`SOL_FUNDING_ALCHEMY_RPS` 預設為 8（Alchemy 共用預算），各自按 endpoint 冷卻。`alchemy_grpc` 模式使用 `SOL_FUNDING_WORKERS=4` 個 funding worker，create 串流保留獨立 worker；同錢包依序檢查，活躍交易不會因 60 秒保留期到期被重複執行。WebSocket 混合交易模式維持單 worker，避免改變買入處理順序。
`features.app check` 的 `funding_history_provider` 顯示實際選用來源；不輸出帶 key 的 URL。
入金缺口只補查 CEX／Privacy Cash 來源：開始時固定最近 120 秒（或更短的 backfill 設定）為時間窗口，每來源每輪最多 3 頁、每頁 100 筆。超過 300 筆保留游標，約 1 秒後續查並讓其他來源先處理；成功續查不累加失敗退避。每頁工作與游標一起落庫，全部補完才推進完成位置，重啟後接續原缺口。RPC 失敗沿用冷卻／退避；窗口不隨等待縮短，但新缺口仍只從最近窗口開始，並不保證補回長時間斷線的全部歷史。資格期限與 create 即時性限制不變，歷史補查不會追買。
新 hotlist 錢包不掃描歷史；gRPC 重連直接監控新交易，不重播斷線期間的買單。
Create 狙擊必須有本次程序的即時接收紀錄；補查及重啟恢復的交易只保存資料，不追補買入。
入金資格仍檢查地址交易筆數上限及已支援的發幣紀錄；不再限制入金前 30 天的簽名活動。
processed 分叉核對及自己已送出訂單的確認仍保留。
Funding discovery 亦辨認 `cex_addresses.json` 內交易所的原生 USDC 出金；
`SOL_HOTLIST_MIN_FUNDING_USDC=50`、`SOL_HOTLIST_MAX_FUNDING_USDC=800` 是預設值，兩端包含。
以 1 USDC = 1 USD 作金額門檻，不查即時美元匯率；SOL 入金繼續使用原有 SOL 門檻。
只接受 [Solana 官方列出的 USDC mint](https://solana.com/docs/payments/how-payments-work)
`EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`，支援 SPL Token `transfer`／`transferChecked`（含 inner instructions）。
以同筆 confirmed 交易的 token balance owner、CEX 簽名及收支餘額證明出金，無需額外 RPC 查 mint／owner／報價。
收款 token account 的擁有人才是 hotlist 錢包，仍須通過 system wallet、交易筆數及發幣紀錄檢查；
若錢包地址不在該筆帳戶列表，以已證實的入金 slot 作歷史檢查依據，同 slot 簽名活動本身不再阻擋入場。
範圍按每筆轉帳指令判斷，raw amount 為六位小數 USDC；持久化 event 以 `:USDC` 區分 SOL。
設定、即時監控範圍及一萬地址連線測試見 [Alchemy hotlist 訂閱](docs/alchemy-hotlist-stream.md)。
公共 RPC 僅適合短暫探測，不能承諾訂閱上限／延遲／零漏單。環境變數優先於 `.env`。
Telegram 填本專案 `.env` 的 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`，不自動讀上層憑證。

`SOL_HOTLIST_TTL_HOUR=24` 表示 hotlist 有效 24 小時；`SOL_AUDIT_RETENTION_HOUR=72`
表示審計保留 72 小時。兩者支援小數小時，程式會換算成秒。
`SOL_SLIPPAGE_PERCENT=2` 表示整條路徑的最終最低收幣量至少為即時預估輸出的 98%。
第一段使用半份滑點保留 quote，Pump 只花該保證數量；多換到的 quote 留在錢包，不會為湊單花用既有 quote。
`SOL_MIN_POOL_LIQUIDITY_SOL` 檢查 DLMM 的 WSOL reserve；報價亦要求能完成全額交換。
原生 SOL 路由直接按 bonding curve 與當前費率報價，以 `buy_exact_sol_in` 花設定的 SOL 預算，
`min_tokens_out` 使用同一個滑點百分比。DLMM reserve 門檻不套用到這條原生 SOL 路由。

DRY_RUN 在達標時組未簽名交易及模擬，不簽名、不送單，不建立虛假的已成交持倉。
未設定買入錢包私鑰時，DRY_RUN 使用訊號錢包作模擬付款人；設定私鑰後使用推導出的公鑰模擬自己的資金狀態。
LIVE 需要在 `.env` 的 `SOL_WALLET_ADDRESS` 填入 Base58 私鑰（64-byte keypair），程式自動推導公鑰，
模擬通過且訊號未過期後才簽名、保存、送單。買額不包含交易費與新 ATA 租金。

## 買入路由範圍

Pump 支援上述 DLMM → Pump 和原生 SOL → Pump 路徑；曲線已畢業、池的 quote 不符、活躍 transfer hook、轉帳費、
暫停／預設凍結帳戶、流動性不足、交易超過 1232 bytes、模擬失敗均拒絕。
支援普通 SPL 和可公開轉帳的 Token-2022（包含 SPCX 的未啟用 hook、未暫停擴充）。
使用訊號交易的 lookup table 作壓縮提示，帳戶及數量由 SDK 重新推導。
CU 上限按完整交易模擬消耗自動加 20% 餘量；沒有另設優先費參數。
`check` 會列出 `meteora_dlmm_to_pump_curve`、`pump_native_curve`、`sol_to_pump_curve`、`sol_to_stonk_curve` 及 `stonk_native_curve`。
`check` 的 `jupiter_access=keyless` 表示免 key 模式，不代表 API、流動性或 LIVE 實測已通過。
DRY_RUN 的 `trading_enabled=false`。Stonk 本地路徑亦檢查第一段池的 WSOL reserve，並套用相同流動性門檻。

Stonk SOL 回歸樣本：`39FLtNUXE65aTrBBxtTPQ8t2bPkHBoft9gqBDwnPndnVhGXGMNs3Tvy5bge5A7ddUEAn4G5gGDLM7nEUHN9NHmDM`。
樣本花費 4.34 SOL，扣除目標幣 1% 轉帳稅後實收 102384232.409726 枚；租金、小費不計入買入金額。
目前池已畢業，歷史池狀態重建僅供離線測試；買入路由會拒絕該池。賣出另支援下述 Stonk CPMM 遷移。
Stonk 非 SOL 回歸樣本：`2R6jsVXbZN2CRN59VwHysxgadAgropybVwk7ou2DQYjDLJ74CFqgf7cmMhCkYNZ5cDJk9Ki3Un17mxjBwKXffaDT`。
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
快取過期、距觀察到的 slot 超過 32 或失效時，建單會按需查主／後備 HTTP 取得新 hash；兩邊失敗才拒絕建單。買入仍須通過原有訊號時效限制。
背景 blockhash HTTP 更新仍保留；取消的是帳戶的定時 HTTP 輪詢。

交易期間，入金歷史補查、預熱、Jupiter 背景尋路及背景 blockhash 更新會暫停發起新請求；獨立的交易對帳繼續運作。
已送出的請求可以完成，WebSocket 更新繼續接收。Node 建單完成後，退避持續至 Python 模擬／送單結束。
背景預熱佇列最多 128 個，重啟預熱最近 32 個；舊代幣身分／路徑永久保留，冷門代幣可能需要重新補齊帳戶。
沒有把所有曾見過的地址永久訂閱；HTTP 主端失敗時可使用 Alchemy 後備。
health 的 `quote_cache` 顯示帳戶及 blockhash hits／misses。

快取完整時，建單及送單路徑省去原本兩次即時 blockhash 查詢；仍保留 SDK 模擬、Python 模擬及
`sendTransaction` preflight。首次代幣或 slot 落後仍可能增加帳戶讀取。
這能縮短等待，但公共 RPC 延遲、processed 訊號到達時間及 leader 排程仍影響成交 slot；沒有同 slot 保證。

Stonk 首次以 Jupiter `maxAccounts=32` 尋找 Whirlpool／Meteora DLMM 路徑，保存池與 lookup table 地址。
其後依最新池資料使用 Orca／Meteora SDK 本地報價及組指令，快取命中不必再等 Jupiter。
只接受單一路徑、最多三段；拒絕拆單、循環及未知場地。每段只花上一段的保證輸出，多出的中間代幣留在錢包。
Pump 非 SOL Create／買入共用 quote 路徑快取並預熱換幣帳戶；已知 DLMM 前段可作路徑提示，其他 quote 自動尋路。

預設限制（皆為百分比，可在 `.env` 覆蓋，精度 0.01%，0 表示不容許）：

- `SOL_MAX_QUOTE_TOKEN_TAX_PERCENT=3.9`：報價幣的轉帳稅上限。
- `SOL_MAX_TOKEN_TAX_PERCENT=3.9`：目標代幣的轉帳稅上限。兩項獨立比較，任一嚴格超標拒絕，相等通過。
- `SOL_MAX_PRICE_IMPACT_PERCENT=2`：扣除費用後，相對各池當下邊際價格的合計價格影響超過 2% 拒絕。
- `SOL_SLIPPAGE_PERCENT=2`：保留原有最終實收 minOut，至少為完整即時預估淨輸出的 98%。

平台、協議、creator 及池費不再作為拒買／拒賣門檻；仍在報價中扣除，以正確計算淨實收及 minOut。
不再檢查往返總費用，也不將兩種幣的稅率相加。Telegram 超標通知列出報價幣或目標代幣、檢查稅率、上限及對應設定。
價格影響上限仍只用於非 SOL 路由買入、Stonk 買入及背景換幣，未新增至 Pump SOL 直接買入或緊急賣出。
這些是建單前的成本上限，與鏈上的 minOut 各自生效；不把高費用藏在滑點內。
兩個轉帳稅上限預設各 3.9%，可設 0–20%，精度 0.01%；檢查當前及已排程的名義稅率，
即使 maximumFee 封頂令本次實際扣稅較低，名義稅率超標仍拒絕。
Stonk 買入、曲線／畢業後賣出分別檢查報價幣及目標幣；背景換回 SOL 使用報價幣上限。
Whirlpool／DLMM 報價及 Stonk 買賣支援轉帳稅的淨額計算；Pump 曲線上的帶稅 mint 仍需專用適配，
未知／啟用中的 hook 仍不放行。稅率檢查重用組單取得的 mint／epoch。
本地檢查不是鏈上 buy guard：快照之後的設定變更仍由原有 minOut 限制實收，不能保證執行時稅率上限。
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

probe 不開資料庫、不簽名，最多抽 10 筆；`--tx` 列出交易版本、入金與 Create 解碼，
`--raw` 可列出成功解析的公開原始交易。`--mint` 只驗來源。
`--buy-route` 只重新報價及模擬未簽名原子買入，不簽名、不送單、不寫資料庫。
歷史簽名只作路徑提示；若代幣現已畢業，會拒絕。不要用 probe 當高頻監控。

## systemd

VPS 新部署使用 [`deploy/sol-follow.service`](deploy/sol-follow.service)：
固定 `/home/ubuntu/sol-follow`（ubuntu 登入後的 `~/sol-follow`），以 `ubuntu` 使用者執行，開機啟動、失敗後 10 秒重啟，
5 分鐘內連續失敗 5 次便停止重試。安裝後的 service 名稱為 `sol-follow`；
同一部署只啟動一個實盤實例。

以下以 Ubuntu 24.04／26.04 LTS／Debian 12、有 sudo 權限的 SSH 帳戶 `ubuntu` 為例；
把 `VPS_IP` 換成實際 IP；若 SSH 使用者不是 `ubuntu`，同步修改 unit 的 User／Group 及所有 `/home/ubuntu` 路徑。
systemd 的路徑使用絕對路徑，不填 `~`。Ubuntu 24.04 使用 Python 3.12，Debian 12 使用 3.11；
Ubuntu 26.04 使用 uv 安裝獨立 Python 3.12，不更改系統 Python。以下安裝 Node.js 22 LTS。
Node 安裝方式依 [NodeSource 文件](https://github.com/nodesource/distributions/blob/master/DEV_README.md)，
版本生命週期見 [Node.js Releases](https://nodejs.org/en/about/previous-releases)。
程式不提供 HTTP 服務，無須新增應用程式入站 port；需能出站存取 RPC、WebSocket、gRPC、Jupiter 及 Telegram。

### 1. 從 Windows 打包及上傳

在本專案 PowerShell 執行。此包不含 `.env`、私鑰、資料庫及 Windows 依賴。
`cex_addresses.json`、`take_profit_rules.json` 雖被 Git 忽略，卻是啟動必要檔案，打包時保留。

```powershell
tar.exe -czf sol-follow-vps.tar.gz --exclude=sol-follow-vps.tar.gz --exclude=.git --exclude=.venv --exclude=node_modules --exclude=.env --exclude=wallets --exclude=data --exclude=__pycache__ --exclude=.pytest_cache --exclude=.codex --exclude=.agents .
scp .\sol-follow-vps.tar.gz ubuntu@VPS_IP:~/sol-follow-vps.tar.gz
ssh ubuntu@VPS_IP
```

這是新建資料庫的部署流程。如果是搬遷正在使用的實例，先停止舊服務，再另外完整搬移
`data/`（含 SQLite、WAL、SHM、訂單與持倉）及所需錢包；新 VPS 啟動前確保舊實例已停止。
單機 lock 不會阻止不同 VPS 同時使用同一錢包。

### 2. 在 VPS 安裝執行環境

以下指令均在 VPS 的 Bash 執行。Node 安裝到系統 PATH，systemd 不會載入互動 shell 的 nvm 設定。

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip build-essential ca-certificates curl nano
curl -fsSL https://deb.nodesource.com/setup_22.x -o /tmp/sol-follow-node-setup.sh
sudo bash /tmp/sol-follow-node-setup.sh
sudo apt-get install -y nodejs
python3 --version
node --version
npm --version

install -d -m 750 /home/ubuntu/sol-follow
tar -xzf "$HOME/sol-follow-vps.tar.gz" -C /home/ubuntu/sol-follow --no-same-owner
install -d -m 700 /home/ubuntu/sol-follow/data /home/ubuntu/sol-follow/wallets
cd /home/ubuntu/sol-follow
```

接著依系統選擇一種方式建立 Python 環境。若服務已在運行，先執行 `sudo systemctl stop sol-follow`。

**Ubuntu 24.04／Debian 12，新建環境：**

```bash
# Ubuntu 24.04；Debian 12 改用 python3.11。
python3.12 -m venv .venv
```

**Ubuntu 26.04，或先前誤用 Python 3.14：**

先以 `Ctrl+C` 結束仍在編譯的 pip，再以 `ubuntu` 使用者執行以下指令（uv 不加 sudo）。
依 [uv 官方文件](https://docs.astral.sh/uv/guides/install-python/) 安裝獨立 Python 3.12；
舊 `.venv` 改名保留，`.env` 及專案資料不受影響。

```bash
curl -fsSL https://astral.sh/uv/install.sh -o /tmp/sol-follow-uv-install.sh
UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 sh /tmp/sol-follow-uv-install.sh
"$HOME/.local/bin/uv" python install 3.12

if [ -d .venv ]; then
  mv .venv ".venv-backup-$(date +%Y%m%d-%H%M%S)"
fi
"$HOME/.local/bin/uv" venv --managed-python --python 3.12 --seed .venv
.venv/bin/python --version
```

應顯示 `Python 3.12.x`。uv 管理的 Python 也是 service 執行時需要的檔案，請保留其安裝目錄。
service 仍使用 `/home/ubuntu/sol-follow/.venv/bin/python`，無須修改路徑或啟用虛擬環境。

**環境建立成功後，安裝依賴：**

```bash
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install --only-binary=:all: -r requirements.txt
npm ci --omit=dev
```

以上以 `ubuntu` 登入執行，無需另建 `sol-follow` 帳戶；若依賴安裝失敗，先處理錯誤再繼續。
若專案已位於 `~/sol-follow`，跳過解壓；曾用 root／其他帳戶建立的專案需先修正擁有者：
`sudo chown -R ubuntu:ubuntu /home/ubuntu/sol-follow`。
不要沿用從 Windows 搬來的 `.venv` 或 `node_modules`。
`--only-binary=:all:` 要求預編譯套件；若 Python／平台不相容，會直接報錯，避免長時間編譯。
若日誌出現 `cp314` 或 `Building wheel for grpcio`，先取消安裝，確認 `cat /etc/os-release`
及 `.venv/bin/python --version`，再以 3.11／3.12 重建虛擬環境。保留舊環境備份，不改系統 Python。

### 3. 設定環境變數

只在首次部署複製範例；更新時保留現有 `.env`。

```bash
cp -n .env.example .env
chmod 600 .env
nano .env
```

第一輪保持 `DRY_RUN=true`。範例明確選擇 `alchemy_grpc`／`processed`，必須填入自己的
`ALCHEMY_API_KEY`。若暫不用 Alchemy，把這兩項改為 `SOL_FEED_MODE=auto`、
`SOL_HOTLIST_COMMITMENT=confirmed`，並讓 key 留空；不能保持 gRPC 模式而不填 key。

需要 Telegram 時填 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`。
RPC 可用程式預設值，或自行加入 `SOL_RPC_HTTP_URL` 與 `SOL_RPC_WS_URL`。
兩個 RPC 地址各自設定；不要將 Windows 路徑放進 VPS 設定。
檢查 `SOL_BUY_AMOUNT_SOL`、`SOL_SLIPPAGE_PERCENT` 等交易設定；範例的明確值會覆蓋程式 default。

```bash
.venv/bin/python -B -m features.app check
```

先確認輸出有 `mode: dry`、`trading_enabled: false`、正確 feed、門檻及買入金額。
`check` 驗證本地設定／JSON，不代表 RPC、Telegram 或買入路由已通過網路測試。

### 4. 安裝並啟動 service

```bash
sudo install -m 644 deploy/sol-follow.service /etc/systemd/system/sol-follow.service
sudo systemd-analyze verify /etc/systemd/system/sol-follow.service
sudo systemctl daemon-reload
sudo systemctl enable --now sol-follow
sudo systemctl status sol-follow --no-pager
sudo journalctl -u sol-follow -n 100 --no-pager
sudo journalctl -u sol-follow -f -o cat
```

`active (running)` 只表示程序存活；在日誌核對啟動設定及後續 health 的連線／訂閱狀態。
`Ctrl+C` 只退出 journal 追蹤，服務仍會繼續。systemd 啟動前亦會執行 `check`。
`.env` 由 Python 自行讀取，不需要 `source .env` 或額外 `EnvironmentFile`。
service 使用 `ProtectHome=read-only`，可讀取 home 下的程式及設定，只有 `data/` 可寫；
私鑰存於 `/home/ubuntu/sol-follow/.env`，由 `ubuntu` 擁有、權限 `600`。

### 5. 切換 LIVE（準備實盤時才做）

先停止服務，在 VPS 用 `nano ~/sol-follow/.env` 填入買入錢包的 Base58 私鑰。
`SOL_WALLET_ADDRESS` 雖然名稱含 ADDRESS，但內容是私鑰；不填公鑰、助記詞或 JSON 陣列。
不需要另外建立 keypair 檔案；公鑰由程式推導。`.env` 由 `ubuntu` 擁有，執行 `chmod 600 ~/sol-follow/.env`。

```dotenv
DRY_RUN=false
SOL_WALLET_ADDRESS=填入買入錢包的Base58私鑰
```

再次執行 `check` 後 `sudo systemctl start sol-follow`。這一步才允許系統簽名及實際買入；
成功訊息分為 `buy submitted`（已送出）與 `buy finalized`（最終成交核對完成）。
目前只支援 dev 超標自動退出；一般止盈／止損及不支援的退出路由仍需人工處理。

### 維護、更新及排錯

```bash
sudo systemctl stop sol-follow
sudo systemctl start sol-follow
sudo systemctl restart sol-follow
sudo systemctl disable --now sol-follow
sudo journalctl -u sol-follow --since "30 minutes ago" --no-pager
```

以上是各自獨立的管理指令，按需要執行。修改 `.env` 後 restart 即可；修改 service 後需先 daemon-reload。
若因多次失敗被停止，修正原因後執行 `sudo systemctl reset-failed sol-follow` 再 start。
`203/EXEC` 通常是 Python 路徑／權限錯誤；`217/USER` 是帳戶不存在；
`226/NAMESPACE` 要檢查 `data/` 是否存在。`node` 找不到時確認系統安裝，而非只在 nvm 裡。
缺少兩個必要 JSON、gRPC key 留空、WS 模式卻設 processed，會在 check 階段失敗。

更新前先 stop，並完整備份 `data/`；例如在 VPS 登入帳戶的 home 建立私有備份：

```bash
sudo systemctl stop sol-follow
install -d -m 700 "$HOME/sol-follow-backups"
tar -C /home/ubuntu/sol-follow -czf "$HOME/sol-follow-backups/data-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" data
```

上傳並解壓新版程式，保留 `.env`、`wallets/`、`data/`；重新執行 pip install、npm ci、check。
新版 unit 亦需 install、verify、daemon-reload，然後 start。不要刪除資料庫以處理啟動問題。
本機 Windows 無 systemd，範本未在本機實際啟動；VPS 上的 verify、check 及日誌檢查是部署驗收步驟。

舊訂單與持倉保留在資料庫。新路由的已簽名訂單會恢復成交核對；未簽名的中斷訂單取消。
舊路由訂單及一般止盈／止損仍需人工處理；新跟買支援下述 dev 超標緊急退出。

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
常駐 create 備援走 confirmed logs + confirmed getTransaction，仍可在此模式觸發合資格的新 create；備援證據會明確記為 confirmed，較遲到達的 processed 副本不會降級它。

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

gRPC 交易必須通過即時 slot／區塊時間錨點檢查，不能把收到歷史交易的時間當成新交易。
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

## Create 買前 dev token 數量檢查

`SOL_DEV_MAX_HOLDING_TOKENS=60000000`：在預留買單前，用已收到的 create 交易決定是否買入。
Pump `buy`／`buy_v2` 有指定 token 數量時，calldata 超標可立即拒絕；exact-in 類型則用同筆完整交易的 token metadata 核對實收。
不另外呼叫 `getMultipleAccounts` 或 `getTokenAccountsByOwner`，不再「查詢中先買」。
嚴格超過上限就不買；等於上限可通過。數量未知／資料不完整也不買，不會當成零。
同筆交易若轉出、賣出或燒毀 dev token，會計回已消耗數量，避免只看末尾餘額而低估。
判定對象是實際簽名、命中 hotlist 的 launch user；metadata 的 creator／費用接收人另存作證據。
市值已取消；自己的買額、滑點、費用、流動性、模擬與成交核對沿用。
完整規則見 [Create 狙擊模式](docs/dev-create-sniping.md)。

既有已簽名訂單、持倉及超標退出核對保留；新 create 已在買前被拒絕，不需等背景結果才賣出。

背景事件即時喚醒退出工作，等待鏈上買入入帳／賣出確認時每 2 秒核對。

Pump／Stonk 畢業前曲線支援直接賣回原報價幣，共用 `SOL_SLIPPAGE_PERCENT` 計算 minOut。
非 SOL 緊急賣出先收原報價幣；Stonk 已有 WSOL 帳戶會保留 WSOL，避免關閉原有帳戶。
賣出 finalized 後將本次實收報價幣／WSOL 排入下述背景換 SOL 佇列。
Stonk 已遷移到 Raydium CPMM 時，賣出會自動切換到核驗後的 CPMM 池，SOL／非 SOL 報價幣皆支援。
Pump 曲線完成後，賣出會自動尋找官方 canonical PumpSwap 池，SOL／非 SOL 報價幣皆支援。
不能保證即時成交：遷移尚未完成、池子不可用、流動性不足、滑點或 RPC／模擬失敗時會通知並延後重試。
Stonk 遷移到非 CPMM 場所、非 canonical PumpSwap 池及一般止盈／止損仍未支援。
簽名交易在送出前持久化，送單逾時／重啟只核對及重送同一簽名，未知結果不另簽賣單，避免重複賣出。
只有已確認 finalized 失敗才重新建單。買賣確認順序不同時亦會正確更新持倉。
`DRY_RUN=true` 不送出買賣交易，超標退出只記錄通知。

緊急賣出亦遵守單池費、總費及轉帳稅上限；超標不簽名、不送單，保留退出任務並通知暫緩，
之後重新報價重試。因此費率一直超標時不保證能立即退出；不會為緊急賣出自動繞過上限。

新 create 的數量資料不完整或不相符時，會停止該筆入場，不另查持倉 RPC 或等待補齊後追買。舊版尚未完成的背景檢查在重啟時標為無法判定。
這些情況不能證實超標，所以**不會自動賣出已持有的代幣**，會發出無法判定通知。

結果保存在 `trading.sqlite3` 的 `token_dev_holdings`，跨重啟保留且不隨審計清理刪除。
新 create 已消耗 hotlist，後續重播不重查、不重買；已排除的 mint 維持排除。
省略參數預設 6,000 萬；留空僅停用新檢查，`0` 表示不容許任何正數 dev launch token 數量。
提高上限或停用檢查不會解除已存的排除結果；降低上限以首次快照重新比較，不另發 RPC。
首次超標或無法判定會記錄日誌，Telegram 已配置時亦會排入通知。

## Stonk 畢業後 CPMM 緊急賣出

參考交易：`4nCR6soQt3ngbFLYsstthF6pUk6ZWwsdHrvXhGLUi2Cs4kwjt7Eu18KEtGe93dsWmCdETUKeT2RdixukRTLdJ8SX`。
鏈上 slot 454149770，第一段經 Jupiter CPI 呼叫 Raydium CPMM `swap_base_input`：
`7y4NAbrNJbc1xQjtsZoNrhoKwJjBTxG8euKPTiX5nC1w` → `Xs3oZwbHvqis4NYcf4YKWmEia2eC84wSiVrcYcTqpH8`，
賣出 19,222,990.8786 枚，扣 1% 輸入轉帳稅後取得 0.30244461 枚報價幣。
樣本後續經 Raydium CLMM／Orca／其他場地兌回 SOL；本專案直接組 CPMM 賣回原報價幣，
不複製樣本的 Jupiter 多池拆單、佣金、優先費或帳戶，原報價幣再交由閒置背景換幣流程處理。

每次建賣單驗證原 LaunchLab pool PDA、Stonk platform、base／quote 及 status=2、migrateType=1；
從已驗證的平台 cpConfig 推導排序後的 canonical CPMM pool，核對 pool/config/vault/oracle 的
owner、discriminator、mint、token program、authority、swap 狀態及鏈上 Clock 的開池時間。
若平台設定變更而無法定位這個 canonical 池，暫緩並通知，不盲目搜尋或接受任意池。
建單重算池 reserve，扣除 protocol／fund／creator 待領費用，按官方整數公式處理
creator 在輸入或輸出收費及 Token-2022 輸入稅；套用現有 pool／total／tax 上限及共用滑點。
報價幣轉帳稅仍不支援。模擬確認精確賣出本次跟買數量、保留舊持倉，非 SOL／保留 WSOL 時核對實收 minOut。
沒有新增參數或 Jupiter 報價請求；`check` 的 `emergency_sell_routes` 會列出 `stonk_cpmm_to_quote`。

測試包含真實交易事件／指令帳戶比對、後續帳戶快照、合成 SOL 池、費率及偽造帳戶拒絕、
遷移途中暫緩及重試；均為離線測試，並非真實資金買賣或主網模擬成功紀錄。
公式來源：[Raydium CPMM swap](https://github.com/raydium-io/raydium-cp-swap/blob/master/programs/cp-swap/src/instructions/swap_base_input.rs)、
[費用與曲線計算](https://github.com/raydium-io/raydium-cp-swap/blob/master/programs/cp-swap/src/curve/calculator.rs)。

## Pump 畢業後 PumpSwap 緊急賣出

參考交易：`tVDdek1fVJM28zTAUDyY2yM8XuBkrCiRL516jhwMmCfQo8ubuZtdyRuawEr4JVS6rurqW9unJo7hdvtZiVgZ61j`。
slot 454200320，`F77SVJjnJ9da6CYPvrhxg2LAxjRMCKozV8q7PSVzpump` 在官方 PumpSwap 池
`884eMYZpeKYqYT7zeuHTxnjHt1ioJ9nhNUTLRSQDzgSf` 賣出 14,213,352.207018 枚，實收 0.619363185 SOL。
樣本使用 `sell(base_amount_in, min_quote_amount_out)`，其 min-out 為 0；本程式不沿用該數值，
每次按最新淨報價及 `SOL_SLIPPAGE_PERCENT` 設正數 min-out。

先驗證原 Pump 曲線 PDA、owner、complete 及原報價幣，再推導 index=0、creator 為
`pumpPoolAuthorityPda(mint)` 的 canonical PumpSwap pool；不掃描任意同名池或複製舊交易。
驗證 pool/global/fee-config owner 與 discriminator、LP mint、base/quote vault 的 ATA、mint、owner
及 token program；全局停賣、池尚未遷移、餘額或費率不符時暫緩並通知。
以固定版本官方 `@pump-fun/pump-swap-sdk@1.20.0` 離線組單，包含當前 pool-v2、creator、buyback
及適用的 cashback 帳戶；舊版池需要 extend_account 時在同一交易內處理，由模擬驗證。

報價使用實際 vault reserve 加上 `virtualQuoteReserves`，並確認真實報價幣 reserve 足夠支付。
LP、protocol、creator 費計入單池費及總費上限；buyback 是 protocol 費內部分拆，不再重複相加。
Pump 的 base／quote 轉帳稅與活躍 hook 仍拒絕。只賣本次跟買實收，不動原有代幣；
SOL 對沒有既有 WSOL 時原子解包，有既有 WSOL 則保留帳戶及舊餘額，只將本次新增 WSOL 排入背景解包。
非 SOL 對先賣回原報價幣，沿用已知路徑的閒置背景換 SOL 及其費率限制。
賣出不需要 Jupiter 報價或新參數，`check` 會列出 `pump_swap_to_quote`。

測試重現上述真實 SellEvent、虛擬 reserve、協議費分拆及指令帳戶；非 SOL、舊版池及 cashback
使用明確合成的池狀態測試。沒有進行真實資金交易或主網模擬，VPS 需更新程式後重啟。
協議來源：[PumpSwap 官方文件](https://github.com/pump-fun/pump-public-docs/blob/main/docs/PUMP_SWAP_README.md)。

## 背景換回 SOL

背景換 SOL 的任務保存在 `trading.sqlite3.quote_sweeps`，重啟後繼續處理。
只使用每張賣單實收的報價幣數量，不掃空原有錢包餘額。沒有進行中／待確認／結果未知的買單、
賣單或 dev 退出任務時才準備及送出；新買賣任務到來會取消未簽名的背景準備，恢復閒置後重新報價。
已送出的鏈上交易無法撤回；其結果未知時保留原簽名核對，不重簽或同時執行另一張換幣單。

使用已知 SOL→報價幣路徑反向計算，重新讀取池帳戶，用 Whirlpool／Meteora DLMM 本地 SDK 建單，
不沿用舊報價或盲目接受外部路由指令。找不到路徑、流動性不足、費用／價格影響超標時保留餘額，
30 秒後再於閒置期間嘗試，不為換幣放寬門檻。

`SOL_MAX_QUOTE_TOKEN_TAX_PERCENT` 限制待換回 SOL 的報價幣轉帳稅，平台／池費沒有額外上限。
`SOL_MAX_PRICE_IMPACT_PERCENT` 另外限制價格影響。
背景換幣的滑點取 `SOL_SLIPPAGE_PERCENT`、`SOL_MAX_PRICE_IMPACT_PERCENT`
兩者最小值，逐段設鏈上 minOut；簽名前要求報價不超過 5 秒並再次模擬。
費率上限依報價快照檢查，鏈上由 minOut 約束實收；不是在鏈上鎖住池子的費率設定。
這些限制能約束報價成本及成交下限，但不保證沒有 MEV 或報價前已發生的池價操縱。

WSOL 以臨時帳戶在同一交易解包回 SOL，不關閉原有 WSOL ATA；模擬亦檢查原有資產未被額外消耗。
多段路徑可能留下少量中間幣餘額；完成後只將本交易增加的部分排入後續轉換，不消耗舊持倉。
費用過高、dust 或未知送單結果可能令餘額繼續保留，日誌／Telegram 會標示暫緩、送出及完成狀態。

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

## 2026-10-11：買入觸發改為 dev create

目前服務固定使用 `dev-create`，不再以多 hotlist 錢包買入投票觸發。
上文涉及目標買方金額／票數的舊策略敘述只適用舊版；相關設定不再控制入場。
本人買額、滑點、流動性、DRY_RUN 與成交核對設定沿用；市值取消，dev token 數量改為買前本地判定。
實際簽名的 launch user 命中 hotlist 即消耗資格；純 create 也支援。
完整觸發、去重及分叉恢復規則見 [Create 狙擊模式](docs/dev-create-sniping.md)。

## 自動止盈與止損

服務會監察已確認買入的自身持倉，依根目錄 `take_profit_rules.json` 執行分段止盈、止損、保本與移動止盈。止盈固定啟用，使用 `SOL_TAKE_PROFIT_POLL_SECONDS` 和 `SOL_TAKE_PROFIT_RULES_PATH` 設定；dry-run 不賣出。規則完成與剩餘持倉只在 finalized 成交核對後更新。以成交均價記錄入場 index，使用池子 spot index 觸發止盈；監察滿 30 分鐘後，檢查間隔放慢至 5 倍（預設由 5 秒變成 25 秒），重啟與部分賣出不重設計時。持倉代幣／交易池的 confirmed 交易事件會直接喚醒該代幣的 spot index 檢查，不用等待下一次輪詢；定時檢查仍作補漏。

完整語義、盈虧報價基準、重啟與部署方式見 [自動止盈說明](docs/take-profit.md)。規則檔被 Git 忽略，部署時須另外複製至 VPS 並重啟服務。

## Dev 賣出保護

Dev 賣出該代幣任何數量，即觸發跟賣剩餘自身持倉的 100%。使用 confirmed 交易證據、獨立事件佇列及既有緊急賣出執行器；普通轉帳不觸發，dry-run 不實際賣出。詳見 [Dev 跟賣](docs/dev-sell.md)。
