# 實作決策與現行範圍（2026-10-05）

## 現行服務

目前服務啟用 CEX／Privacy Cash 原生 SOL 入金、資格檢查、SQLite hotlist、Pump／Stonk Create 解碼、
WebSocket 即時入口、歷史補查、持久化 jobs／cursor、有限重試、finalized 入金核對、
資料庫容量管理與 Telegram／健康統計。交易 adapter、報價、組單、簽名廣播、
自動跟買／賣出、止盈止損與在途成交對帳均未啟用。
`check` 與啟動通知明確列出空的 execution venues 和 trading_enabled=false。
DRY_RUN=false 仍不會讀私鑰或送單。

通用策略事件、計票／預留、持倉儲存和止盈規則計算保留，供未來新路由使用；
沒有服務入口會呼叫它們產生新交易。既有訂單和持倉資料不刪除、不修改狀態；
啟動時發現未完成訂單或非零持倉會通知人工核對。

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
  Pump bonding curve／PumpSwap、LaunchLab curve、SOL／非 SOL quote 買賣、多跳仍待實作。

來源規格：[Pump 官方文件](https://github.com/pump-fun/pump-public-docs/blob/main/docs/PUMP_PROGRAM_README.md)、
[LaunchLab layout](https://github.com/raydium-io/raydium-sdk-V2/blob/master/src/raydium/launchpad/layout.ts)、
[LaunchLab PDA](https://github.com/raydium-io/raydium-sdk-V2/blob/master/src/raydium/launchpad/pda.ts)。
公開帳戶證據保存在 `tests/fixtures/launchpad_origins_mainnet.json`。

## 保留的保障

收款人須 on-curve、System owner、空 data、非執行帳戶、非黑名單，入金金額合格。
Privacy Cash 只支持指定 native program／pool 及明確提款語義，不支援 USDC 提款。
以 signature／instruction path／recipient 去重；hotlist TTL 使用鏈上入金時間。
缺交易／時間／metadata 或 RPC 失敗不假裝成功，保留有界重試。
歷史游標在候選持久化後推進，給緊急補查及舊缺口有限處理份額。
SQLite 容量清理不刪有效證據、未完成 jobs／orders、持倉或永久成功去重。

## 驗證

離線測試覆蓋 funding、legacy／v0／v1 交易讀取、來源偽造與快取、通用策略／儲存、
Pump／Stonk SOL 與非 SOL Create、CPI、四個主網樣本、重播／最終性與拒絕錯誤資料，
資料清理、補查游標及本地 HTTP／WebSocket 服務啟動與取消。
另驗證停用交易後仍能新增 hotlist、發射台交易不計票／預留、LIVE 設定不讀私鑰／送單，
舊在途訂單保持原狀。以當前 pytest 執行輸出為準；不代表實盤成交驗收。
Privacy Cash 公開 fixture 仍驗證收款人收到 29.889 SOL（pool 減少 30 SOL、fee 0.111 SOL）。
