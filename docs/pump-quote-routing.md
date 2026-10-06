# Pump 非 SOL 報價幣路由

參考 RH `rh_follow_buyer.py::_discover_and_cache_gmgn_buy_route_once`：依 pair token
發現直達／中轉池，快取路徑，再以當前狀態計算買額。SOL 服務維持獨立，不 import RH runtime。

- 解碼器從 Pump v2 指令的 quote mint／token program 判斷付款幣，不設 mint 白名單。
- 頂層及有完整 stackHeight 的 CPI 買入均需付款、曲線 vault 出幣及收款餘額證據；重複買方／mint 的歸屬不明時拒絕。
- 目標買入不必附帶 SOL→quote 的 DLMM swap；可以花既有 quote，或在其他場所先換幣。
- 每個 quote mint 的路徑寫入 quote_recipes；不同目標 mint 可重用。Pump Create 即開始背景尋路／預熱。
- 自己固定使用 SOL_BUY_AMOUNT_SOL，經已核驗的 Whirlpool／Meteora DLMM 路徑（最多三跳）換 quote，再以 Pump exact-in 買入。Raydium CPMM 未加入。
- 每一段只花上一段保證輸出；整筆交易有最終 minOut，並檢查單池費、總費、price impact 和模擬實收，不消耗既有 quote／中轉幣餘額。
- 目標買額上限沿用 SOL_FOLLOW_MAX_TARGET_BUY_SOL；非 SOL 估值只用快取，缺失便跳過，不在訊號判斷時查報價 API。
- 帳戶／ALT／blockhash 快取及交易優先退避維持原有行為。首次或冷路徑需背景預熱，不能保證首次訊號就可買入。

Pump 新路由目前拒絕 base／quote 的轉帳稅及活動 transfer hook 等未實作擴充。
無支援換幣路徑、無流動性、畢業曲線、費用／滑點超標、超過 1232 bytes 都不送單。
因此「所有 token」是按有效 mint 通用處理，並非保證每個 mint 在任何池況都可成交。

測試包括原有 SPCX 買入及 Create CPI 樣本；USDC／DJT 直達及多跳使用已保存的真實換幣池快照，
Pump 曲線配對／儲備為明確標示的合成測試資料，不代表已完成這些配對的主網實盤驗證。

Pump v2 quote account 及 amount 規格：[Pump 官方 Buy V2 文件](https://github.com/pump-fun/pump-public-docs/blob/main/docs/instructions/BUY.md)。
