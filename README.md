# sol-follow

獨立 Solana 服務：CEX／Privacy Cash 原生 SOL 入金 → 資格檢查 → SQLite hotlist，
並解碼監聽交易中的 Pump／Stonk Create。
群體跟買與分批止盈／止損是待接入新交易路由的策略目標。
**沒有修改、import 或共用 RH/BSC 的 runtime、錢包和資料庫。**

發射台範圍固定為 **pump.fun 和 stonk 旗下代幣**。以下地址只作來源驗證樣本，不是 mint 白名單：

- pump.fun：`CBLx6CRcCTtbmgTdxpqnF2dP1MpWbMUjngtNbFTApump`
- stonk：`49tVXDe7c44LGg95brseq1KfzFr4SvoYwDMrm1Xn7zC6`

發射台來源使用鏈上帳戶證據驗證；名稱、`pump` 後綴或交易場所都不能作證。
`launchpads/` 負責來源識別，DEX adapter 放在 `chain_common/venues/`。
stonk 目前識別樣本所屬的 LaunchLab platform；未驗證的其他 platform／舊版發射路徑不自動納入。

**目前沒有啟用任何交易 adapter。** 自動跟買、賣出、止盈止損、簽名送單及
在途訂單成交對帳均已停用。服務保留 CEX／Privacy Cash 入金資格、hotlist、交易取得／補查、
持久化佇列及健康通知；pump.fun／stonk 鏈上來源核驗可透過 `--mint` probe 使用。
**Pump／Stonk 的 SOL 與非 SOL 對 Create tx 解碼已接入**，支援頂層／可驗證 CPI、
legacy／v0／v1，保存 mint、creator、quote、curve／pool 及名稱等欄位。
四個指定主網樣本已加入回歸 fixture；詳見 [Create 解碼範圍](docs/create-tx-decoding.md)。
Pump bonding curve／PumpSwap、Stonk LaunchLab curve 及 SOL／非 SOL quote 買賣路由仍待實作。
舊訂單／持倉資料保留；啟動時若有未完成訂單或持倉，會通知需人工核對。

## 啟動 DRY_RUN

Python 3.11+；在 **sol-follow 目錄** 執行，不能在上層 token_alert 執行同名 package。

```bash
cd /home/ubuntu/sol-follow
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
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
正式運行請填 `SOL_RPC_HTTP_URL`、`SOL_RPC_WS_URL`，建議背景另配 endpoint。
公共 RPC 僅適合短暫探測，不能承諾訂閱上限／延遲／零漏單。環境變數優先於 `.env`。
Telegram 填本專案 `.env` 的 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`，不自動讀上層憑證。

目前 DRY_RUN 和 LIVE 均運行監聽／入金資格及 Create 解碼，不讀私鑰、不簽名、不送交易。
`DRY_RUN=false` 不會重新啟用交易；必須另行實作和接入買賣路由。

## 交易路由重新接入前

目前不能用切換 LIVE 作成交驗收。交易路由重新接入後，需重新核驗 Create／買賣解碼、
組單、成交對帳、私鑰保護、滑點／費率及既有狀態恢復，再以專用錢包驗收。
`check` 會列出 `execution_venues=[]` 和 `trading_enabled=false`。

## 指令

```bash
.venv/bin/python -m features.app stats
.venv/bin/python -m features.probe --mint CBLx6CRcCTtbmgTdxpqnF2dP1MpWbMUjngtNbFTApump
.venv/bin/python -m features.probe --mint 49tVXDe7c44LGg95brseq1KfzFr4SvoYwDMrm1Xn7zC6
.venv/bin/python -m features.probe --samples 3
.venv/bin/python -m features.probe --tx SOLANA_SIGNATURE
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

probe 不開資料庫、不讀私鑰，最多抽 10 筆；`--tx` 列出交易版本、入金與 Create 解碼，
`--raw` 可列出成功解析的公開原始交易。`--mint` 只驗來源。
目前沒有交易報價或模擬買入選項；不要用 probe 當高頻監控。

## systemd

把整個資料夾獨立部署到 `/home/ubuntu/sol-follow`，安裝依賴、設定 `.env`，先 `mkdir -p data`。
檢查並按實際路徑修改 `deploy/sol-follow.service` 的 User／WorkingDirectory／ExecStart／ReadWritePaths，然後：

```bash
sudo cp deploy/sol-follow.service /etc/systemd/system/sol-follow.service
sudo systemctl daemon-reload
sudo systemctl enable --now sol-follow
sudo journalctl -u sol-follow -f
```

舊訂單與持倉保留在資料庫；目前服務不再自動恢復、成交對帳或退出這些持倉。

## 容量與保障邊界

`data/funding.sqlite3`、`trading.sqlite3`、`audit.sqlite3` 分庫；總 DB/WAL/SHM 預算預設 2 GiB，
90% 提前清理／通知，目標 1.5 GiB。增量回收空頁，不在交易期做完整 VACUUM。
這是保護有效證據的**軟上限**，不是無條件刪資料的硬限額：若容量全是有效 hotlist／未完成任務／持倉／
去重證據，就告警而不刪除它們；真正磁碟滿會停止無法持久化的交易，不能保證繼續買。
審計容量優先可淘汰，不代表完整歷史計數。WAL + NORMAL 遇斷電可能丟最後已提交資料；一般 service 重啟不受影響。

策略／取捨和測試範圍見 [docs/DECISIONS.md](docs/DECISIONS.md)，原設計見 [IMPLEMENTATION.md](IMPLEMENTATION.md)。
