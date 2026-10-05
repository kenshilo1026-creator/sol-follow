# sol-follow：Solana CEX 入金錢包群體跟買

## 1. 本次交付與專案目標

2026-10-05：目前保留監聽、入金資格、SQLite、来源核驗及 service；交易 adapter 已移除。
群體跟買、交易恢復及止盈止損是待重新接入交易路由的設計要求，並非目前啟用功能。
Pump／Stonk 的 SOL／非 SOL Create tx 解碼已接入，見 `docs/create-tx-decoding.md`。
啟動方式、驗收結果和實作範圍見 [README.md](README.md) 及 [docs/DECISIONS.md](docs/DECISIONS.md)。
以下保留設計要求；若與初稿的「待實作／建議」用詞不同，以前述交付文件及 `.env.example` 為準。
`cex_addresses.json` 複製自現有 Robinhood 專案的 `share_common/cex_addresses.json`；
`take_profit_rules.json` 複製自現有專案根目錄。保留原有資料，不轉換數值、不改動來源檔案。
程式按用途分包，不讀取上層 RH/BSC 的設定；私鑰、資料庫與 `.env` 不提交 Git。
目前本地 `.env` 預設 `DRY_RUN=true`，已接入設定載入器及 native SOL Privacy Cash decoder。

目標流程：

```text
CEX 出金 → 驗證實際收款 → SQLite hotlist
                                 ↓
                   監聽 hotlist 錢包的真實買入
                                 ↓
                 同一 mint 在窗口內有 ≥N 個不同買家
                                 ↓
                   原子預留 → 風控 → 送單 → 確認
                                 ↓
                      持倉監控／止盈止損
```

這不是「hotlist dev 發幣就買」：觸發條件是 hotlist 內的**不同錢包買入同一 token mint**。
不以 token 名稱、symbol、logo 或 pool address 作 token 身分。
MVP 建議只接受 Solana CEX 直接 SOL 出金，以及已支援交易場所的 SOL/WSOL 買入；
USDC 等 SPL 入金、間接轉帳、跨鏈及更多交易場所後續分批加入，不把未支援資料誤判為成功。
暫不附加 Robinhood 的「從未發幣」、EVM nonce 上限或既有 12 條資金指紋等額外限制。

## 2. 目錄與責任

```text
sol-follow/
├── IMPLEMENTATION.md
├── .env               # 本地設定；Git ignore
├── cex_addresses.json
├── take_profit_rules.json
├── launchpads/        # pump.fun／stonk 鏈上來源識別與範圍限制
├── share_common/     # 設定、Telegram、單一實例鎖
├── chain_common/     # Solana RPC、交易正規化、快取、venues/ DEX adapter
├── features/
│   ├── funding/      # 入金 decoder、WebSocket／歷史補查
│   ├── database/     # 三個独立 SQLite、容量／保留清理
│   ├── strategy/     # 群體訊號、持倉／止盈止損
│   └── runtime/      # service、資格 worker、健康與恢復
├── wallets/          # 本地私鑰（ignored），README
├── data/             # 運行時 SQLite／鎖（ignored）
├── tests/            # 離線 fixture 與回歸測試
├── docs/             # 決策與驗收
└── deploy/           # sol-follow.service
```

沿用原專案的 `features/` 複數命名。後續按用途新增子目錄，不把所有邏輯塞進一個主程式。
JSON 保留根目錄原檔；運行資料夾由程式建立，不以 `.gitkeep` 保存。
`sol-follow` 是專案資料夾名稱；从其目錄以 `python -m features.app` 啟動。

## 3. 參考現有專案，但不直接 import 舊 bot

下列路徑相對於目前上層 `token_alert` 專案，供開發時閱讀，不是新專案的執行依賴：

| 參考位置 | 可借用的設計 | 不可照搬的部分 |
| --- | --- | --- |
| `bsc_bot/cex_tracker.py`、`bsc_bot/storage.py` | 出金證據、hotlist、事件去重、訂單狀態保存 | EVM 地址轉小寫、wei、ABI、區塊掃描與 nonce |
| `features/funding/funding_scheduler.py`、`qualification_health.py` | 限流、有限 worker、佇列進度、最新工作優先 | EVM getLogs 區塊範圍與舊資料庫結構 |
| `features/database/maintenance.py` | 小批清理、短鎖、超時退讓、記錄實際刪除量 | 不經容量測量便套用舊上限或共享舊資料庫 |
| `rh_follow_buyer.py`、`trade_execution/`、`bsc_bot/buyer.py` | DRY_RUN、執行／確認分離、持倉恢復、成交通知 | buyGuard、curve ABI、Permit2、Universal Router、EVM gas／nonce |
| `take_profit_rules.json` | 止盈止損類型、相對入場收益率、分批賣出語義 | 直接把 EVM 市值／價格來源套到 Solana |

不 import `rh_follow_buyer` 來偷用設定，避免匯入時啟動舊資料庫、worker 或錢包行為。
優先沿用 Python 的模組化與非同步服務結構；Solana client／簽名套件在正式實作時另行選版、鎖定版本及測試。

## 4. CEX 出金 → hotlist

1. 讀取新專案本地 `cex_addresses.json` 的交易所分類。只載入能解碼成 32-byte 公鑰的 Solana Base58 地址，
   **保留大小寫**；略過 EVM 地址及 `robinhood_direct_withdrawals` 專用區段。相同地址重複分類要提示，不默默選一個。
   複製的清單包含多鏈資料，不代表所有項目適用 Solana，也不保證 CEX 地址永遠有效。
2. 即時入口用 `logsSubscribe` 的 address mentions；通知只是候選 signature，不把「提及 CEX」當成出金。
   標準 mentions 每個訂閱只支援一個地址，大名單需分配訂閱預算；供應商批次 stream 可作後續選項。[1]
3. 每個 signature 共用一次交易取得／解析工作，要求交易成功。解析頂層及 inner instructions，
   證明來源是清單內 CEX、實際接收方及金額；不能只靠 fee payer 或帳戶餘額上升推定付款方。
4. MVP 只支援已驗證的 System Program SOL transfer 形態；同一筆批次出金可建立多位收款人證據。
   去重鍵使用 `signature + instruction path + recipient + asset`，不能只用 signature 吃掉其他收款人。
   排除自轉、已知 CEX 互轉、建立 token account 的 rent、關帳退款及未識別的程式資金移動。
5. 收款人需通過金額上下限、地址黑名單及已支援帳戶類型檢查。未知 owner／PDA／託管帳戶先標未知，
   不當成普通交易錢包。後續 SPL 出金必須解析 token account 的錢包 owner，不把 token account 本身放進 hotlist。
6. 短交易中保存精簡出金證據與 hotlist entry，提交後更新記憶體索引及監聽清單。TTL 依鏈上入金時間計，
   不是每次 replay 延長；新入金可依設定續期。缺 blockTime 時保留 slot／時間未知狀態，不冒充即時入金。

補漏以每個來源的 `getSignaturesForAddress` 分頁游標進行，搭配 `getTransaction`，不掃創世區塊。
游標必須在候選已持久化後才推進；RPC 失敗或 null 不是「沒有交易」，保留有限重試。[2][3]
新 hotlist 生效後補查「入金至訂閱成功」的小窗口，避免監聽建立期間漏掉買入；過期訊號只用於審計，不追價。
即時與補查共用去重，同時給補查有限處理份額，避免永久飢餓；超出設定覆蓋窗口要明確通知缺口。

## 5. 辨識錢包買入：不能只看收到了 token

為支援的 launchpad／DEX program 寫獨立 adapter，統一輸出：
`signature、slot、block_time、instruction_path、wallet_owner、mint、side、quote_mint、quote_raw、token_raw、venue、confirmation`。

2026-10-05 範圍修正：只納入 pump.fun／stonk 旗下代幣，使用者提供的兩個 mint 是辨認平台的樣本，
不是只交易兩個 mint。發射台來源與交易 DEX 分開：來源由 `launchpads/` 核驗；目前沒有啟用的交易 adapter，買賣計票、退出及在途成交對帳均停用。
目前已完成來源 gate，Pump／PumpSwap／LaunchLab 的交易 adapter、非 SOL quote 路由、
Token-2022 與 creator fee 支援仍待實作；不能將來源辨識成功視為可交易驗收。

- 以已知 program 的 instruction 語義、必要 account 關係、CPI／inner instructions、付款與收款證據共同判定 buy/sell。
  代幣轉帳、空投、mint、NFT、流動性增減、WSOL 包裝／解包，都不能直接計作買入。
- 完整處理 legacy／v0 訊息與 address lookup table 載入地址；account index 必須對應展開後帳戶列表。
  `getTransaction` 明確指定支援版本；不支援版本或缺少 metadata 時保留未知。[2][4]
- Token account 不等於錢包。依 pre/post token balances 的 mint／owner 及可信帳戶資料歸戶，
  同一 owner 的多個 token accounts 合併。SOL 支出必須扣除交易費、rent 等非 swap 支出；不能把總 lamport 減少當成買額。[4]
- 多跳 swap 僅計最終目標 mint；中間資產不加票。多筆同 mint 的合法買入可保存事件，
  但同一錢包在同一統計窗口只佔一票。無法拆分複合交易就不投票，留下原因。
- MVP 先以 `confirmed` 成功交易投票，之後追蹤 finalized 並處理回滾。processed 可作預熱線索，
  不直接觸發正式跟買；標準 WebSocket 不是 EVM 公共 pending mempool，也不保證任何交易都即時可見。
- 只覆蓋已實作並測試的 program／路由。未知 wrapper 不猜買賣方向；記錄 program、signature、限速告警，供後續補 decoder。

## 6. N 個不同 hotlist 錢包 → 同一 mint 跟買

以下是初版語義；目前 N=3、W=120 秒，可在 `.env` 修改：

- `N` 是不同錢包 owner 數，整數且 ≥1；統計時間為可設定的滾動窗口 `W`。
  例如 N=3、W=120 秒只是測試示例，正式值由使用者決定。
- 僅計買入時已符合 hotlist 入金資格、且觸發時仍有效的錢包。買入前的舊交易不能因稍後收到 CEX 出金而補算。
  異步補查以鏈上先後順序校驗；同 slot 順序不清楚時補齊交易順序，不能靠本地接收順序猜測。
- 每張票保存 hotlist 入金依據、最新有效買入時間及唯一事件 ID。重複 feed、補查重播、同 signature 重播都不刷新時間。
  同 owner 真正再次買入可以刷新其一張票。使用鏈上時間／slot 排序，另保留本地 observed time 測延遲。
- 建議買入後若確認該錢包已完全賣清，撤銷其票；部分賣出保留。若持倉無法證實，MVP 保守不計入活躍票。
  這是防買完即賣的選項，與「窗口內曾買過」策略不同，需在設定中明確標示。
- 計數到 N 時在記憶體產生候選，SQLite 以 `(strategy_version, mint, mode)` 唯一鍵原子預留。
  不能把 N 筆重複買入當 N 個人，也不能讓兩個 worker 同時送單。
- MVP 建議同 mint 成功跟買後不自動再入場；冷卻或 rebuy 是後續獨立選項。
  預留、送出未知、成功和失敗要分開：確定未送出／已失敗才能依策略釋放，RPC timeout 不能立刻重新買。
- DRY_RUN 走同一偵測、計數、風控和去重，但只寫模擬決策，不簽名廣播；live／dry-run 名額分隔。
  切換 live 不自動執行歷史達標訊號，等待新的有效觸發並重新檢查時效。

同 CEX 資金不等於不同真人；女巫拆錢包仍可能達到 N。未來可加入共同入金來源／同批出金群組權重，
但不要未經同意直接移植 RH 指紋規則，改變本策略的有效票数。

## 7. 交易執行與止盈止損

買單路徑只讀已準備的 hotlist、票數、mint／pool、路由和費率快取；缺必要安全資訊時延後或跳過，
不在這裡同步跑完整 CEX 歷史或寫大型審計 JSON。先做一個交易場所的單次買賣，再加入其他 adapter。

Solana 交易需重新實作 instructions、簽名、recent blockhash、Compute Budget 與 priority fee：
不搬用 EVM nonce、gas price、approve、Permit2。背景預取 blockhash 和有效高度、餘額與帳戶狀態，
送單前確認新鮮度；必要時建立 ATA、處理 WSOL、保留 fee/rent 所需 SOL。
依官方建議保持 blockhash／preflight commitment 一致，以有效 block height 判斷過期，不硬寫固定秒數。[5]

交易保護至少包含：設定買額、滑點 minOut、報價時效、總費用／priority fee 上限、價格偏移與流動性限制、
mint／freeze authority 及 Token-2022 擴充能力檢查。不支援的 transfer fee/hook 等不能套用普通 SPL 計算。
對外部組單結果校验 payer、mint、收款帳戶、支出上限、可呼叫 program 及簽名需求，不能盲簽任意交易。

狀態機建議：`reserved → signed → submitted → confirmed → finalized`，另外有 `failed / expired / unknown`。
送出前持久化 signature 與交易意圖；RPC 接受不等於成交，必須查 signature 狀態及實際 token 收款。[6]
重送相同已簽 bytes 與換 blockhash 重簽是兩回事；未排除原單成功前不得盲目重簽，避免買兩次。
重啟先恢復在途單，保留每錢包 SOL 預算預留／賣出互斥，不能假設沒有 EVM nonce 就沒有併發衝突。

`take_profit_rules.json` 沿用原檔语義：`profit_over_entry_percent`、`current_position`、stop_loss、protect、trailing。
例如 profit 300 表示較入場價上升 300%（4 倍），sell 7 表示當時持倉的 7%，不是初始持倉的 7%。
入場價由實際 quote 支出／token 實收計算；費用另記，收益計算是否含費要固定一致。
止盈以可執行賣出報價／可信 pool 價格判定，不單憑 UI 市值；部分賣出後只更新剩餘持倉和已執行規則，
不可把後續止盈檔位全部清掉或重新定錨。每個持倉版本＋規則有唯一觸發鍵，確認成交後才標完成。
MVP 不實作人氣反彈、dev sell 跟賣或自動補買。

## 8. SQLite、效能與容量

不共用 RH／BSC 的 SQLite 或 lock file。建議未來在 `data/` 分成：

| 資料庫 | 主要資料 | 原則 |
| --- | --- | --- |
| `funding.sqlite3` | CEX transfer 精簡證據、hotlist、jobs、checkpoints | 入金提交與 hotlist 同庫原子化 |
| `trading.sqlite3` | 有效 buy votes、signal reservations、orders、positions、TP 執行狀態 | 短交易、買賣優先，不做重掃 |
| `audit.sqlite3` | 決策、錯誤、處理時間摘要 | 異步、小批寫入、容量可淘汰 |

資料庫分開後不能假裝跨庫具有原子性。funding 提交後發布帶版本通知，trading 以事件 ID 冪等消費；
重啟／遺失通知用持久化 outbox 或增量版本對帳重建 hotlist 快取。
熱路徑以 `mint → {wallet: vote}` 記憶體索引計數；重啟從尚在窗口內的精簡事件恢復，不全表掃描。
索引包括 hotlist 到期、funding recipient/time、votes mint/time、jobs state/due、order signature 唯一鍵。
raw token amount 用十進位字串／任意精度整數，避免 SQLite signed integer 或浮點精度溢出。

買賣最高優先，funding 次之，補查／清理最低；設不同 RPC 配額、HTTP pool、有限併發及共享 429 退避。
只要新任務持续增加就必須比較每分鐘新增／完成／待處理增量，不能用「有 heartbeat」代表追得上。
SQLite 用 WAL、每個 connection 正確配置 busy timeout／同步等級；若採 NORMAL，需明示斷電可能丟失近期提交，
並靠鏈上 signature 對帳恢復，不能宣稱絕不丟單。資料庫交易內不做 HTTP、RPC、Telegram。

容量計算包含主檔、WAL、SHM、pending/outbox，另設所在磁碟剩餘空間水位。軟上限提前通知並清理至較低目標；
只刪過期 raw cache、已完成 job、舊審計及不再依赖的資料，保留有效 hotlist、票、在途單、持倉、封鎖和必要去重證據。
大 JSON 完成精簡證據交接後即棄；批次刪除有時間預算，checkpoint／壓縮避開交易尖峰。
刪除後頁面可供 SQLite 重用，但檔案不一定立即縮小；VACUUM 要有額外空間，不能在快滿時盲做。
沒有安全可清資料時必須告警，暫停非必要紀錄；真正磁碟滿時不能保證繼續安全持久化／買入，禁止無紀錄送單。

## 9. 設定與觀測

完整預設見 `.env.example`。使用此專案自己的 `.env`，不讀舊 bot 的私鑰。

### 已加入的 Privacy Cash 來源設定

根目錄 `.env` 已設定：

```dotenv
DRY_RUN=true
SOL_HOTLIST_SOURCE_CONTRACT=9fhQBbumKEFuXtMBDw8AaQyAjCorLGJQiS3skWZdQyQD
SOL_PRIVACY_POOL_ADDRESSES=4AV2Qzp3N4c9RfzyEbNZs2wqWfW4EwKnnxFAZCndvfGh
```

`SOL_HOTLIST_SOURCE_CONTRACT` 是允許的 Solana **program ID** 清單，逗號分隔且保留大小寫；
`SOL_PRIVACY_POOL_ADDRESSES` 是 Privacy Cash 原生 SOL 資金池，不是 program、CEX 或 fee payer。
decoder 同時核對 program、對應資金池、提款方向／收款人、成功交易及 lamports 變化，
不能只憑地址被提及或一般 System Program transfer 判定出金。通過後以 `provider=privacy-cash` 保存實收 SOL。
這是 CEX 直接出金之外的獨立來源分支，不代表能識別匿名入池前的 CEX，也不自動支援 USDC 提款。
沒有自行啟動正式 service 或送實盤交易。`.env` 是本地忽略檔，部署要另外帶過去。

### 其餘設定

| 設定 | 用途／初版建議 |
| --- | --- |
| `DRY_RUN` | 預設 true |
| `SOL_RPC_HTTP_URL`、`SOL_RPC_WS_URL` | 交易／即時讀取 endpoint |
| `SOL_BACKGROUND_RPC_HTTP_URL` | 背景補查限額可獨立；同供應商帳戶仍可能共用配額 |
| `SOL_HOTLIST_MIN_FUNDING_SOL`、`SOL_HOTLIST_MAX_FUNDING_SOL` | 直接 CEX SOL 入金篩選，使用十進位安全轉換 |
| `SOL_HOTLIST_TTL_SECONDS` | 地址有效期；上線前指定 |
| `SOL_FOLLOW_MIN_WALLETS` | N；上線前指定 |
| `SOL_FOLLOW_WINDOW_SECONDS` | W；上線前指定 |
| `SOL_FOLLOW_REQUIRE_REMAINING_POSITION` | 建議 true；全賣後不再算有效票 |
| `SOL_SIGNAL_MAX_AGE_SECONDS` | 不追歷史／補查晚到訊號 |
| `SOL_BUY_AMOUNT_SOL`、`SOL_SLIPPAGE_BPS` | 買額、滑點；上線前指定 |
| `SOL_MAX_PRIORITY_FEE_LAMPORTS` | 優先費總額上限，區分每 CU 單价 |
| `SOL_BACKFILL_MAX_AGE_SECONDS` | 補漏窗口，例 3600 秒；跳過缺口必須可觀測 |
| `SOL_DB_MAX_BYTES`、`SOL_DB_TARGET_BYTES` | SQLite 家族總預算及清理目標；依磁碟實測設定 |
| `SOL_AUDIT_RETENTION_SECONDS` | 審計時間保留；容量優先淘汰時要標註報表不完整 |

Telegram 共用新專案 `share_common/` 的設定來源，訊息加 `[SOL]`；私鑰和 token 不進日誌／Git。
健康報表至少列：feed 連線、已覆蓋游標／鏈上延遲、交易 null／429、未知 program、
funding 和解析隊列的新增／完成／逾時、hotlist 數、達標票數、送單／确认時間、DB 容量和清理量。
只在真實故障／缺口／持續積壓時告警，正常 drained 或短暫無新資料不叫故障；相同故障節流並附恢復通知。

## 10. 分批實作及驗收

1. **離線 decoder**：取得實際 CEX SOL 出金、批次出金、買／賣、空投、NFT、v0／CPI 樣本；先確定第一個支援的 program，
   不預填未经核實的 program ID。證明能解析真正來源／owner／mint／買賣方向。
2. **只讀 discovery**：先跑 DRY_RUN，落地 funding、hotlist、去重、補漏和容量清理；不送單。
3. **群體訊號**：用歷史排序及亂序重播驗證 N/W 語義、全賣撤票、熱名單新增競態、重啟恢復；不補買陳舊訊號。
4. **單次交易**：實作一種 venue 的買入／賣出、滑點與費用保護、過期／timeout／重啟對帳，先模擬與受控測試。
5. **完整跟買**：連接原子預留和執行器，再加入 take-profit 引擎、持倉恢復、Telegram 和 systemd。
6. **壓力測試**：feed 中斷、429、RPC 落後、隊列洪峰、DB busy／低磁碟，同時測交易優先、補查公平性和告警。

關鍵回歸案例：同錢包 10 筆買入只算一票；不同錢包同 mint 達 N 只送一單；同名不同 mint 不混；
失敗交易／空投／WSOL 包裝不投票；同 signature 多路 feed 不重複；補查沒有跳頁；
RPC 送單 timeout 不重複下單；partial sell 後後續 TP 正常；DRY_RUN 不簽名不廣播；磁碟滿不靜默丟證據。
性能用實測 p50/p95/p99 分開記錄解析、計數、DB 預留、組單、send ACK、鏈上確認；
send ACK 不是鏈上成交，不預先保證固定毫秒門檻或全網零漏單。

## 官方技術參考

核對日期：2026-10-05；正式實作時再核實所選 RPC 供應商限制與 launchpad／DEX 版本。

1. [Solana logsSubscribe](https://solana.com/docs/rpc/websocket/logssubscribe)
2. [Solana getTransaction](https://solana.com/docs/rpc/http/gettransaction)
3. [Solana getSignaturesForAddress](https://solana.com/docs/rpc/http/getsignaturesforaddress)
4. [Solana RPC JSON structures](https://solana.com/docs/rpc/json-structures)
5. [Solana confirmation and expiration](https://solana.com/developers/cookbook/transactions/confirmation)
6. [Solana sendTransaction](https://solana.com/docs/rpc/http/sendtransaction)
