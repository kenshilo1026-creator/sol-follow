# 實作決策與現行範圍（2026-10-05）

## 現行服務

目前服務啟用 CEX／Privacy Cash 原生 SOL 入金、資格檢查、SQLite hotlist、Pump／Stonk Create 解碼、
WebSocket 即時入口、歷史補查、持久化 jobs／cursor、有限重試、finalized 入金核對、
資料庫容量管理與 Telegram／健康統計。另啟用 Pump 畢業前非 SOL quote 的
Meteora DLMM → Pump 原子買入、hotlist 計票／預留、模擬及新買單的 finalized 核對。
`check` 與啟動通知列出 `meteora_dlmm_to_pump_curve`；DRY_RUN 只模擬，LIVE 才讀本地 keypair 簽名廣播。
賣出、止盈止損、Stonk 買入與其他交易場所仍未接入。

簽名交易以 FULL durability 保存後才送出；逾時／查不到結果保留簽名及 mint 預留，不重簽重買。
DRY_RUN 保存 dry-simulated，不捏造已成交持倉；LIVE finalized 後以 token balance delta 入帳。
既有其他路由訂單不修改，啟動時提示人工核對；所有賣出仍需人工管理。
每段共用 `SOL_SLIPPAGE_PERCENT` 推導同一個最終輸出下限，不讓兩段滑點相乘。
第一段保留半份滑點，第二段只花保證 quote，多出 quote 留在錢包。

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
  支援上述畢業前非 SOL quote Pump 買入；PumpSwap、LaunchLab、SOL quote 買入及賣出仍待實作。

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
以當前 pytest／npm test 輸出為準；不代表實盤成交驗收。
Privacy Cash 公開 fixture 仍驗證收款人收到 29.889 SOL（pool 減少 30 SOL、fee 0.111 SOL）。
