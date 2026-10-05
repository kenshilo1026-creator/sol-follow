# 本地錢包

目前服務沒有交易 adapter，不讀私鑰、不簽名、不廣播，即使 DRY_RUN=false 亦如此。
保留既有錢包檔案與設定供未來交易路由使用；不要貼到聊天或提交 Git。
未來 LIVE 路由需使用獨立 Solana 錢包和 Solana CLI 64-byte JSON array，
並重新實作公鑰核對、檔案權限與 symlink 保護。
