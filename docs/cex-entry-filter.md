# CEX hotlist entry filter

CEX funding recipients must pass the existing wallet account checks and a signer-history check before admission. The reference time is the CEX deposit block time, not the current wall clock.

- No prior transactions returned, or no wallet-signed transactions in the preceding 30 days: eligible.
- Incoming transfers and other transactions that only mention the wallet do not count as activity.
- Successful and failed transactions signed by the wallet both count. Activity exactly 30 days before the deposit is still inside the exclusion window.
- The funding transaction itself and transactions in later slots are excluded. Another signed transaction in the same slot stays pending because address history alone cannot establish its execution order.
- The funding signature must appear in address history before admission; an empty or unindexed response does not certify a new wallet. Missing transaction data, timestamps, or RPC failures remain pending in the existing retry queue, subject to its coverage expiry.

History is queried through the existing background RPC. Pagination and checked rows are persisted in SQLite, so retries resume without rescanning completed receipts. Checks are scoped to each funding event. Expired checkpoints are removed by the existing maintenance loop.

This uses the history returned by the RPC; it cannot prove completeness if the provider silently omits older records. Existing hotlist entries are not retrospectively removed. Privacy Cash admission and Telegram behavior are unchanged. No new environment variables are required.
