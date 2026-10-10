# CEX / Privacy Cash hotlist entry filter

CEX SOL/USDC and Privacy Cash SOL recipients must pass the wallet account checks,
transaction-count cap and supported launch-history check before admission.

- `SOL_MAX_PRELAUNCH_TX` defaults to 10. All wallet-address signatures count,
  including incoming, failed, current funding and later transactions. An externally
  proven USDC deposit absent from the owner's account keys is counted once.
- Above the cap, reject before fetching transaction details. At or below it,
  inspect every returned transaction for successful supported Pump/Stonk creates
  attributable to the wallet, regardless of age. A previous launch rejects admission.
- Recent wallet-signed transactions, including failed and same-slot transactions,
  do not by themselves block admission. There is no 30-day inactivity requirement.
- The funding signature must appear in address history unless independently proven
  by the USDC owner balance evidence. Empty or unindexed responses do not certify
  a new SOL recipient. Missing details, timestamps or RPC failures remain pending
  in the existing retry queue, subject to its coverage expiry.

History uses the existing background RPC. Progress is persisted per funding event
in SQLite; retries resume completed work. Policy version 4 invalidates earlier
cached approvals, rejections and pending checks when qualification is invoked again.
It does not automatically reopen completed funding jobs or re-audit existing hotlist entries.

Coverage is limited to history returned for the wallet address and supported
Pump/Stonk decoders; older token-account-only activity may not appear. Funding
amounts, hotlist expiry and Telegram behavior are unchanged.
