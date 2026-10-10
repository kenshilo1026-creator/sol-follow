# 畢業代幣的 dev／create 理論捕捉研究

此程式獨立監控 Pump 及已核驗 Stonk platform 的成功畢業遷移交易，然後回答：
原始 create 裡的 creator（dev）按目前設定的入金與活動規則，能否加入 hotlist；
原始 create 交易能否解碼；同一筆內 dev 的買入能否解析成目前支援的跟買訊號。
**不依賴 sol-follow 的歷史資料庫或實際成交，不啟動交易服務、不模擬、不簽名、不送單。**

## 在 VPS 持續執行

先把本次更新的程式上傳到 `/home/ubuntu/sol-follow`，沿用現有 Python 環境及 `.env`：

```bash
cd ~/sol-follow
mkdir -p data/graduation-research
nohup .venv/bin/python -u graduation_watch.py watch \
  > data/graduation-research/watch.log 2>&1 < /dev/null &
echo $!
```

可以退出 SSH。**此監控與 sol-follow 的 systemd service 分開運行**，不需要重啟交易服務。
同一輸出目錄有獨立程序鎖，不會重複啟動。預設從首次啟動開始持續收集，沒有截止時間。
重啟同一目錄會保留資料、原始開始時間及待查佇列；斷線／停機期間沒有全鏈補查。
舊版的 24 小時研究目錄使用不帶 `--hours` 的 `watch` 啟動，會取消舊截止時間並繼續收集。
新一輪請用另一個目錄，例如 `--output data/graduation-research-20261010`，並相應更改日誌重定向路徑。

Windows 可在專案目錄執行 `.venv\Scripts\python.exe graduation_watch.py watch`；
視窗、電腦及網路需保持運行。長時間收集建議放 VPS。

手動停止（約一秒內保存報告並退出，資料及待查佇列保留）：

```bash
.venv/bin/python graduation_watch.py stop
```

Windows 使用 `.venv\Scripts\python.exe graduation_watch.py stop`。
使用自訂目錄時，`watch`／`report`／`stop` 都要帶相同 `--output`。
若只想跑固定時長，可在全新輸出目錄指定 `watch --hours 24`；固定窗口重啟時保留原截止時間。

## 看結果

```bash
cd ~/sol-follow
.venv/bin/python graduation_watch.py report
tail -n 30 data/graduation-research/watch.log
```

每 30 秒更新：

- `data/graduation-research/report.csv`：每筆成功畢業遷移的 mint、dev、hotlist 判定、create 解碼、create 內買入判定及交易簽名。
- `data/graduation-research/report.json`：完整理由、資金來源、門檻快照、尚未判定的檢查及監控缺口。
- `data/graduation-research/research.sqlite3`：研究專用收集紀錄、公開交易快取、可續查的活動檢查。

`candidate=pass` 的定義是：**create 成功解碼 + dev 通過入金資格檢查 + 同筆 dev 買入路由受支援且通過靜態 SOL 金額門檻**。
`blocked` 表示有明確不符合的條件；`unknown` 表示現有證據不足，不能當作不合資格。
目前非 SOL 買入的美元金額門檻需要當時的價格快取，故即使路由已解碼也會保留 `unknown`。
原始 dev 只從已核驗的 create 取得，不會將畢業執行者、目前被更改的 creator 或池 authority 當成原始 dev。

**pass 不等於一定下單或成交**：現有策略仍要求 N 個不同 hotlist 買方、時間窗口、
市值／dev 持倉／稅率／流動性等條件，以及即時訊號和實际組單。
研究不以今天的價格或儲備偽造歷史模擬，亦不宣稱重建整套群體交易策略。
Create 本身不會觸發訂單；沒有同筆可識別 dev 買入，會標明 `no-supported-dev-buy-in-create-tx`。

## 資料來源及限制

- 「畢業」採 finalized 成功 `migrate`／`migrate_v2` 或 LaunchLab `migrate_to_cpswap`／`migrate_to_amm`，並核驗曲線／pool origin 及畢業狀態；不是只到達滿曲線門檻。
- 使用兩個公共 WebSocket 程式 log 訂閱，先保存 create 線索及畢業候選；只有畢業候選才進行 HTTP 查詢。
  兩個程式的全量 logs 仍有網路流量，公共端點可能斷線或限流。報告記錄缺口，不能保證列出全網所有畢業代幣。
- 找不到即時 create 線索時，按 pool 歷史作有上限查找。預設每地址最多 3 頁；pool 每頁 1000，錢包每頁 100。
- 入金查 dev 錢包及 USDC ATA／現存 USDC token accounts，使用現有 SOL／USDC 金額範圍、Privacy Cash 驗證、hotlist TTL 及 CEX 交易筆數／發幣紀錄規則；不再要求入金前 30 天沒有簽名活動。
  無入金證據、已關閉 USDC 帳戶、歷史超出查詢上限、同 slot 次序不明或 RPC 失敗，都保留未能判定。
- 錢包是否 System account 使用目前 finalized 狀態，沒有 archival account-state 證明；缺失時不直接當作歷史不合資格。
  入金到 create 間已知會移除 hotlist 的 SOL 買入也會排除；非 SOL 中途買入缺少歷史價格則保留未知。
- 額外 HTTP 預設 **0.5 RPS**、每代幣最多 **120 次**，可用 `--rps`、`--pages`、`--max-rpc-per-token` 調整。
  這是獨立程序的限制，會與同 IP 的 sol-follow 用量相加。每次限流／網路錯誤保留佇列，最多 5 次嘗試。
- 預設持續收集，手動停止後不再追加查詢。`pending`／`unknown` 會保留，重新啟動可繼續待查工作。
  只有指定 `--hours` 的固定窗口，才會額外接收 60 秒 finalized 延遲及最多 1 小時尾端查詢；`--grace-seconds` 可縮短此時間。
- 同一輪使用啟動時的門檻；程式中途不重載 `.env`。重新啟動時偵測到已保存的門檻不同會拒絕混用。

ABI 參考：[Pump 官方 IDL](https://github.com/pump-fun/pump-public-docs/blob/main/idl/pump.json)、
[Raydium 官方 LaunchLab IDL](https://github.com/raydium-io/raydium-idl/blob/master/raydium_launchpad/raydium_launchpad.json)。
