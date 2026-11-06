# Native SOL funding lineage

## Scope and RH comparison

Confirmed CEX funding admits root wallet A at depth 0. A successful plain System
SOL transfer from an active A can admit child B at depth 1 after B passes the
existing on-curve, empty System account and complete `prelaunch_activity` checks.
The funding minimum/maximum are the existing SOL amounts. Transfers through arbitrary
programs/CPI, SPL tokens, unsigned sources, exchange destinations and ambiguous
same-slot parent funding are excluded. Privacy Cash has no CEX root and does not
propagate in this implementation. Normal direct CEX/Privacy admission remains available.

RH sources inspected: `funding_discovery.py::_rh_lineage_from_feed`,
`rh_follow_buyer.py::_queue_rh_lineage_child`, `_replay_rh_lineage_edges`, and
`share_common/config.py::normalized_cex_trace_policy` in token_alert.

- RH's current source default is **2** intermediary hops, configurable with
  `CEX_TRACE_MAX_HOPS`. SOL intentionally uses the user's requested **1** maximum.
- RH only propagates plain native payments above its configured minimum, with a
  qualified parent and inherited CEX provenance; recent edges can replay when
  classification completes. Already-hotlisted recipients do not refresh their TTL.
- RH removes a qualified parent at nonce >= 10 (the eleventh sent EVM transaction),
  not after counting ten transfer events. Infrastructure payout senders are exempt.
- Solana has no EVM account nonce. SOL counts **observed confirmed successful plain
  native transfer transactions** from hotlisted lineage members, one per signature,
  persisted across restart. Positive transfers below the admission minimum still
  count. Eleven removes/blocks the parent; repeat receipts and multi-instruction
  transactions count once. Incoming, failed, SPL and program/CPI transactions do not.
  This is not an assertion of complete lifetime outgoing history: pre-upgrade and
  missed transactions outside bounded recovery cannot be reconstructed for free.
- RH's original global 600-second / 2-launch rule is **not used** here. Per the
  revised request, the rule is **one launch per root wallet, without a time reset**.

## One source, one launch

A, B1, B2, B3… share root **A**, not the exchange withdrawal address. Separate
CEX recipients remain separate families. The first valid decoded signing-wallet
create atomically consumes the family in the funding SQLite transaction and removes
all its hotlist entries, including A. This happens before age/quantity/build filters.
Disabled factories also consume; a failed buy does not release the family.
Concurrent creates have only one winner. Restart, fresh funding to a member, and
new children from A cannot reset this state. Repeated child funding does not refresh
hotlist expiry or switch its root. Existing known membership is never reassigned.

The original launch still must pass all existing live, source proof, factory,
funding-age, quantity, quality and execution gates. One source does not guarantee
one successful purchase. A proven invalid chain create releases its own claim,
as existing fork handling does; an executor/build failure never does so. Invalid
parent funding revokes dependent child funding. A parent-transfer-cap tombstone
is retained conservatively even if an observed transfer is later rolled back.

## Timing and recovery

Funding qualification runs after the current transaction's create execution work.
Only confirmed receipts admit children; processed observations use the existing
confirmation follow-up job. No new RPC is added to the create/buy critical path.
Child qualification uses background account/history reads and is not a 0-RPC check.

Recent transfer edges and deduplicated counters persist in the funding database.
Late parent qualification requeues already-observed completed transfer jobs. New
admissions and startup request one bounded background history catch-up for active
lineage wallets (existing 120-second gap window / three pages per turn). This covers
normal subscription latency; it is not continuous polling or unlimited history.
Existing gRPC hotlist coverage and confirmed follow-up carry subsequent transfers.
Missing receipts retry via the existing durable funding jobs. This catch-up and
new-child admission add background RPC usage; the create lane does not await them.

`SOL_LINEAGE_ENABLED=true`, `SOL_LINEAGE_MAX_HOPS=1` (0 or 1), and
`SOL_LINEAGE_MAX_PARENT_TRANSFERS=10` (1..100). Existing active CEX hotlist entries
are registered as independent roots on startup. State lives in funding.sqlite3:
lineage_members, lineage_funding, lineage_consumed, lineage_outgoing, lineage_blocked,
and bounded recent lineage_edges. Member/claim/block state is intentionally retained
so cleanup/restart cannot reset the one-source rule. Turning off new discovery
never erases existing consumed-family safety state.

`dev_audit wallet` now includes lineage, lineage_family, lineage_consumed,
lineage_outgoing and lineage_parent_block. These explain root ownership, the winning
launch signature, and outgoing-cap removal without exposing credentials.

## Distinct-child check budget

Each source root A may start checks for at most **10 distinct child wallets**.
This is separate from the outgoing-transaction cap: one batch transaction with
20 recipients can start only 10 child checks. SQLite reserves `(root, wallet)`
atomically before any account/history RPC (and before local wallet validation).
Rejected wallets, RPC failures and cancelled checks retain their slots. Retrying
the same child, even with a new payment signature, reuses its slot. A retry still
requires the parent to be eligible. Reaching the child budget blocks new checks;
it does not revoke already-admitted children or change the one-launch-per-root rule.

Reservations survive restart, funding renewal, expiry and chain rollback; they
measure work already authorized, not successful admissions. Cleanup never refunds
them. Previously admitted children are seeded on upgrade; failed checks performed
before this budget existed cannot be reconstructed completely. If a legacy source
already has over 10 admitted children, all are retained in the ledger and no new
child checks start. `dev_audit wallet A` exposes `lineage_child_checks`, including
rejected/pending children which never acquired lineage membership. Separate root
wallets have independent budgets.
