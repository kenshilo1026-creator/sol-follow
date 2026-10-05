# sol-follow

獨立 Solana 服務：CEX／Privacy Cash 原生 SOL 入金 → 資格檢查 → SQLite hotlist，
並解碼監聽交易中的 Pump／Stonk Create。
群體跟買已接入一種原子買入路由；賣出與分批止盈／止損仍待實作。
**沒有修改、import 或共用 RH/BSC 的 runtime、錢包和資料庫。**

發射台範圍固定為 **pump.fun 和 stonk 旗下代幣**。以下地址只作來源驗證樣本，不是 mint 白名單：

- pump.fun：`CBLx6CRcCTtbmgTdxpqnF2dP1MpWbMUjngtNbFTApump`
- stonk：`49tVXDe7c44LGg95brseq1KfzFr4SvoYwDMrm1Xn7zC6`

發射台來源使用鏈上帳戶證據驗證；名稱、`pump` 後綴或交易場所都不能作證。
`launchpads/` 負責來源識別，買入路由放在 `trade_execution/`。
stonk 目前識別樣本所屬的 LaunchLab platform；未驗證的其他 platform／舊版發射路徑不自動納入。

**目前支援 Pump 畢業前非 SOL quote 的原子買入：SOL → WSOL → Meteora DLMM → quote token → Pump。**
辨認監聽交易中同一簽名者的頂層 DLMM `swap2` 及 Pump `buy_v2`／`buy_exact_quote_in_v2`，
hotlist 錢包達到 N/W 門檻後，用當前鏈上狀態重新報價、組單；不複製舊交易的數量或指令。
服務仍保留 CEX／Privacy Cash 入金資格、hotlist、交易取得／補查、持久化佇列及健康通知。
pump.fun／stonk 鏈上來源核驗可透過 `--mint` probe 使用。
**Pump／Stonk 的 SOL 與非 SOL 對 Create tx 解碼已接入**，支援頂層／可驗證 CPI、
legacy／v0／v1，保存 mint、creator、quote、curve／pool 及名稱等欄位。
四個指定主網樣本已加入回歸 fixture；詳見 [Create 解碼範圍](docs/create-tx-decoding.md)。
Pump SOL quote、PumpSwap、Stonk 買入、其他 quote 換幣場所、CPI 聚合路由及所有賣出仍待實作。
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
正式運行請填 `SOL_RPC_HTTP_URL`、`SOL_RPC_WS_URL`；所有 HTTP 查詢共用同一個 RPC。
公共 RPC 僅適合短暫探測，不能承諾訂閱上限／延遲／零漏單。環境變數優先於 `.env`。
Telegram 填本專案 `.env` 的 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`，不自動讀上層憑證。

`SOL_HOTLIST_TTL_HOUR=24` 表示 hotlist 有效 24 小時；`SOL_AUDIT_RETENTION_HOUR=72`
表示審計保留 72 小時。兩者支援小數小時，程式會換算成秒。
`SOL_SLIPPAGE_PERCENT=2` 表示整條路徑的最終最低收幣量至少為即時預估輸出的 98%。
第一段使用半份滑點保留 quote，Pump 只花該保證數量；多換到的 quote 留在錢包，不會為湊單花用既有 quote。
`SOL_MIN_POOL_LIQUIDITY_SOL` 檢查 DLMM 的 WSOL reserve；報價亦要求能完成全額交換。

DRY_RUN 在達標時組未簽名交易及模擬，不讀私鑰、不送單，不建立虛假的已成交持倉。
未設定公開錢包時，DRY_RUN 使用訊號錢包作模擬付款人；可設定 `SOL_WALLET_ADDRESS` 模擬自己的資金狀態。
LIVE 需要 `SOL_WALLET_KEYPAIR_PATH`（Solana CLI JSON keypair）及相符的 `SOL_WALLET_ADDRESS`，
模擬通過且訊號未過期後才簽名、保存、送單。買額不包含交易費與新 ATA 租金。

## 買入路由範圍

只接上述 DLMM → Pump 路徑；曲線已畢業、池的 quote 不符、活躍 transfer hook、轉帳費、
暫停／預設凍結帳戶、流動性不足、交易超過 1232 bytes、模擬失敗均拒絕。
支援普通 SPL 和可公開轉帳的 Token-2022（包含 SPCX 的未啟用 hook、未暫停擴充）。
使用訊號交易的 lookup table 作壓縮提示，帳戶及數量由 SDK 重新推導。
CU 上限按完整交易模擬消耗自動加 20% 餘量；沒有另設優先費參數。
`check` 會列出 `execution_venues=["meteora_dlmm_to_pump_curve"]`，DRY_RUN 的 `trading_enabled=false`。

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
