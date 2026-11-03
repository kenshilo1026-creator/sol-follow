# Token 社交連結入場檢查

預設 `SOL_REQUIRE_SOCIAL_LINK=true`。Pump／Stonk、SOL／非 SOL 對的 create
買入都會在組單期間嘗試檢查。URI 取自已驗證 create 解碼，不查鏈上 metadata RPC。
RH Pons 的 calldata 直接帶 socials；Solana 這兩種 create 只帶 metadata URI，
一般須首次 HTTP 讀 JSON，但買入不等待 metadata 回覆。

HTTP 與 unsigned 組單並行，預設最多 `SOL_METADATA_TIMEOUT_MS=400`，亦不超出
原有訊號期限減 150ms 的剩餘時間。組單完成時只讀取已完成的檢查結果：
已解析且沒有社交連結才拒絕買入；尚未完成、逾時或讀取失敗均略過檢查，
照常進行模擬／簽署／送單。未完成的 HTTP 會取消，不等待取消完成才送單。
這是盡力檢查，無法保證每次買入的代幣都有社交連結。
原有訊號新鮮度、dev 賣出、模擬、滑點及 min-out 保護保持有效。

辨識 metadata 的 twitter／x、telegram、discord 等欄位，以及 extensions、
socials、links、properties.links 中的社交網址。至少一條 X/Twitter、Telegram、
Discord 邀請、Instagram、Facebook、YouTube、Reddit、TikTok 或 Warpcast
網址才通過；一般網站、圖片、描述、URI 本身、空值或只有帳號字串都不算。
只檢查宣告的網址格式，不查社群帳號真偽、粉絲數或是否仍存在。

支援 HTTPS/HTTP、ipfs://、ar:// 及內嵌 data:application/json。
HTTP 限制回應大小、重新導向次數，拒絕私有／本機地址及私有 DNS 結果。
每個執行器最多快取 256 個 URI 的判斷、有效 60 秒；HTTP 失敗不快取。
內嵌 metadata 或快取命中可以完全不發 metadata HTTP。

Audit 新增 token-quality 階段，記錄 allowed／blocked／bypassed。
已完成的檢查另記錄社交網域、耗時及 cache_hit。只有 token-social-link-missing
會拒絕；token-metadata-not-ready、token-metadata-timeout、token-metadata-unavailable
均記為 bypassed。Hotlist 沿用 create 被消耗即移除的規則，
品質不合格不會令該 dev 重新入場。停用設定只供明確選擇取消此條件。
