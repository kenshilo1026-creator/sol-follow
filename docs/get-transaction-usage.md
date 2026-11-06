# getTransaction usage

Alchemy's dashboard CU totals are billing units, not request counts. Its method
chart cannot identify Python callers or distinguish this service from another
process sharing the same key.

Runtime callers:
- Service.process: jobs without a stored stream payload, including CEX/source
  signature-only notifications and recovery jobs. FundingRpc is public-first,
  with Alchemy history fallback. Source address pagination is separately
  Alchemy-first and calls getSignaturesForAddress, not getTransaction directly.
- qualify_activity: download each in-cap candidate wallet's history to detect
  prior launches. Over-cap wallets are already rejected before receipt downloads.
- CreateFeed.once: confirmed create backup receipt validation.
- DevSell.once: confirmed receipt fallback for watched wallet events.
- Executor.reconcile_once, DevExit.reconcile, TakeProfit and QuoteSweep: actual fill
  validation. These use execution read endpoints, which may be Alchemy.
- DevExit.buy_evidence: confirmed buy receipt fallback when the processed fill is absent.

TransactionReads now reuses validated confirmed history receipts for up to ten
seconds within one funding job (32 entries / 4 MiB serialized-size budget). It
never shares them across jobs/retries, promotes them to finalized, or caches null,
malformed or processed responses. Each wallet's admission checks still run.
Savings depend on repeated signatures among recipients of the same job; unrelated
transactions still require their normal reads.

Health journal `get_transaction` exposes cumulative physical HTTP attempt counts
since startup for background public, funding history, execution, execution reads,
and confirmed-create backup providers. Compare deltas over equal intervals; these
are not CU totals. Provider source labels contain no endpoint URLs or keys.
`funding_receipt_reuse` reports logical job_requested, history_requested,
history_reused and deposit_reused counts. A logical request can need two provider
attempts, and a reused receipt can save zero Alchemy calls if public would succeed.
The pre-existing deposit reuse is reported separately from the new history reuse.
Manual tools and other processes are outside these runtime counters.

The change does not alter buy/sell execution, confirmation levels, history limits,
RPC provider order, or signal freshness rules. Inspect measured lane counts before
changing provider policy or replacing funding discovery with a full transaction
stream.
