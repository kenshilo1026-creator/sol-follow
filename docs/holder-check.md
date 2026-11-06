# Post-buy holder check

Defaults: `SOL_POST_BUY_HOLDER_CHECK_ENABLED=true`,
`SOL_POST_BUY_HOLDER_CHECK_SECONDS=10`, `SOL_POST_BUY_MIN_HOLDERS=5`.

Like RH's post-fill check, the timer starts when reconciliation first observes
successful confirmed status (or verified finalized fill if confirmed was not
observed). It is not measured from create or submission. The due time and
threshold are stored once per buy in `holder_checks`; restart does not reset them.
Pending checks resume after restart; completed checks do not repeat. Old positions
that finalized before this feature was installed are not retroactively armed.
No holder query, subscription or wait is added before buying.

At the deadline, the background worker requests mint-filtered `getProgramAccounts`
from public RPC, falling back to Alchemy when configured. Each provider attempt
has a four-second budget including background rate/priority waits. Both SPL Token
and Token-2022 are supported: the latter must not be restricted to 165-byte
accounts because extensions increase their size. Only the first 165 bytes are
returned. Positive-balance token accounts are grouped by their token owner, not
counted as separate holders. Dev, our wallet and pool/PDA owners count, matching
RH's all-holder definition; this is not a count of independent human buyers.

A count strictly below N enqueues the existing durable full-position exit.
Exactly N passes. Dev sells and take-profit use the same sell coordination;
existing signed/unknown exits are never replaced. Manually emptied positions are
retired by the existing exit balance check. Native and non-native quote routes
reuse their current sell builders and quote sweep behavior. Notifications name
this reason as popularity, not a dev sale; dev_audit records count, minimum,
provider, context slot, scheduled and actual check times.

Unknown/zero/unindexed, malformed or oversized (10,000+ rows) responses do not
trigger a sale. Responses must include our positive balance and a context at or
after the successful buy. If both providers fail, one notification reports an
incomplete check; the existing exit monitors remain. Providers that disable
mint-indexed getProgramAccounts cannot support this check through that endpoint.

The ten seconds is a scheduling deadline, not a guaranteed sale inclusion time:
active trading has priority, RPC and sell construction add latency. This is a
one-shot current holder snapshot at the deadline, not a historical peak over the
first ten seconds. Tests are offline and do not submit live transactions.

RPC reference: https://solana.com/docs/rpc/http/getprogramaccounts


The shared `BackgroundRpc` read allowlist includes `getProgramAccounts`; both
production provider lanes use this wrapper. Holder failures record at most one
safe attempt summary per provider (reason, elapsed time, optional numeric HTTP
status/RPC code), in both the durable check and decision audit. Telegram reports
these separately. Local allowlist rejection, RPC failure, and invalid/lagging
account data are distinguished; raw exception text and provider URLs are omitted.
An unavailable check remains terminal, so updating the service does not re-arm
old checks or reset their deadline.
