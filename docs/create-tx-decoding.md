# Pump／Stonk Create 解碼（2026-10-05）

Create decoder 已接入現有 CEX／Privacy Cash／hotlist 地址監聽取得的交易，及
`python -m features.probe --tx SIGNATURE`。沒有增加全網 program 訂閱，不能據此
聲稱已捕獲全部發幣。買賣路由維持停用。

## 已支援指令

| 平台 | 指令 | quote |
| --- | --- | --- |
| Pump | create | SOL，標準化為 WSOL mint |
| Pump | create_v2 | 無額外 quote 帳戶為 SOL；指定時支援 SPL／Token-2022 quote |
| Stonk | initialize | 普通 SPL quote，包括 WSOL |
| Stonk | initialize_v2 | SPL／Token-2022 quote |
| Stonk | initialize_with_token_2022 | SPL／Token-2022 quote，base 為 Token-2022 |

Stonk 必須使用已驗證 platform `6BwHHDg3u1854jC8PDLXvR4spTcLNaoBxLJNGC4nTESt`，
其他 LaunchLab platform 不當成 Stonk。支援 legacy／v0／v1 jsonParsed、頂層及具有效
stackHeight 的 CPI；成功 CPI 的 invoke_signed PDA 不要求外層交易簽名。
核對 program、帳戶順序、必要 PDA／vault／token program，以及頂層 mint／user 簽名。
失敗交易、缺 metadata／時間、截斷資料、錯帳戶索引／PDA及未知格式會拒絕。

輸出包括 signature、slot、block time、instruction path、platform／instruction、mint、
quote mint／類型、base／quote token program、creator、user、curve／pool、name、symbol、uri、
decimals 和可識別旗標。creator 為指令宣告創作者，user 為建立操作／付款帳戶；
兩者分開，不用 fee payer 代替 creator，也不把 creator 當成目前 creator-fee 收款帳戶。
metadata URI 不會被執行或下載。

## 四個主網回歸樣本

| mint | 平台／quote | 建立 signature |
| --- | --- | --- |
| CBLx6CRcCTtbmgTdxpqnF2dP1MpWbMUjngtNbFTApump | Pump／SOL | KdvGfThuFAreUz91mDFWDNa3qqm8vznoVHs5QrDK1GD5XiSZj48ZaBNZyehmUPUMh4v5TNuRHb6AMzdmkXpuFx6 |
| 49tVXDe7c44LGg95brseq1KfzFr4SvoYwDMrm1Xn7zC6 | Stonk／Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh | 4aj4cqbCH7rQMdMa5cVVZfJMb8yooYPB3VZ5jbNBuUU1sKEErvVFU5d6M96ptLzT6XprgRHyDbDuRWxwEByCNcPj |
| GcHEn2AHcGDc4Dbhx3bgVotqcrtnvmDwgtfSuT5ytCj | Stonk／SOL | 657YVbMiC6bNqNJGDUZJM9ERkbSipZiFe9LqZwSakZmqRUoUbnfE4G5WvXGGcwM9xgvk5yMBzQAZqNtxLdoUXBTQ |
| 4Nr7xPstQ6VT9MF1kKwmkRy3b1mXct9UPsNgs53Cpump | Pump／SPCXxcqXj6e5dJDVNovHN8744zkbhM2bYudU45BimGb | 3ntiEEU4ADyFya3jAiywQBdiU6Z6hzuBzwnvdr3TujbL4a1uco9WBnfm7gTJAyF4ASoMV8t6auLyEvqEn2N5G9Zr |

fixtures 經已有 Alchemy endpoint 的有界唯讀 getTransactionsForAddress
（asc／jsonParsed／maxSupportedTransactionVersion=1）取得；每 mint 最多取最早 5 筆，
核對成功狀態及實際 Create instruction。fixture 只有公開鏈上資料，不含 endpoint／金鑰。
這四個 mint 是回歸樣本，不是白名單；其他 mint、USDC 與其他 quote 另有合成 ABI 測試。

4Nr7…pump 的 create_v2 在 mayhem／cashback 後只有一個 byte 01，而目前公開 IDL
預期可選 u64 再接 bool。主要 Create 欄位可解碼；此額外 byte 保存於
flags.uninterpreted_tail_hex，creator_fee_bps／holder_reward 為 null，不猜旗標含義。
其他未知／截斷尾部拒絕。Pump 的 mayhem_token_vault 未公開 PDA seed 約束，故不假定
它是 mayhem-state 的 ATA；交易 program 成功執行驗證其帳戶。

## 保存與執行邊界

在 funding.sqlite3.launch_creates 以 signature:path:mint:create 去重；重播不重複
記錄或發出 launchpad-create 日誌。保存先於 job 完成，加入 chain_checks 追蹤 finalized；
狀態為 confirmed／finalized／invalid。RPC null 不是回滾，實際 finalized error 才標 invalid。
已完成且超過 audit 保留時間的記錄可分批清理，待核對 evidence 保留。舊 DB 啟動時新增表，
不刪原有訂單、持倉或入金。服務仍使用一小時 backfill 窗口；舊樣本可由 probe 解碼，
不追補為即時發幣。create+buy 同筆交易只產生 Create evidence，不計買票、不簽名或送單。

```powershell
.\.venv\Scripts\python -m features.app check
.\.venv\Scripts\python -m features.probe --tx 3ntiEEU4ADyFya3jAiywQBdiU6Z6hzuBzwnvdr3TujbL4a1uco9WBnfm7gTJAyF4ASoMV8t6auLyEvqEn2N5G9Zr
.\.venv\Scripts\python -m pytest -q
```

規格：[Pump 官方 IDL](https://github.com/pump-fun/pump-public-docs/blob/main/idl/pump.json)、
[LaunchLab initialize ABI](https://github.com/raydium-io/raydium-sdk-V2/blob/master/src/raydium/launchpad/instrument.ts)、
[LaunchLab PDA](https://github.com/raydium-io/raydium-sdk-V2/blob/master/src/raydium/launchpad/pda.ts)。
規格更新可能改變格式；未知指令需先取得樣本核驗再放行。
