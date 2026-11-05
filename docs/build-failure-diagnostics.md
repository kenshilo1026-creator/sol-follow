# Build failure diagnostics

Unsigned builds retain the existing machine reason (including `sdk-or-rpc-failed`).
`build_diagnostics.exception` adds an allowlisted `category`, `phase`, and optional
Node `code`. Buy failure notices also include this object as `sdk_exception`.

Examples include `range-error / serialize-transaction`, `assertion-error /
ERR_ASSERTION`, and `error / ETIMEDOUT / lookup-tables`. These identify observed
exceptions, not a presumed on-chain failure. Pump 6005 still requires the existing
program-specific simulation evidence. Raw exception messages, stacks, arbitrary
names/codes, nested causes and provider URLs are never exported by this classifier.
The Python IPC boundary validates the same enums again.

Decision details remain limited to 8,000 JSON characters. Small details are
unchanged. Oversized details retain order/stage/signature status, signal age,
simulation error, SDK exception, timing, worker progress and build/RPC summaries
in priority order. Lists retain their latest entries. `detail_truncated`,
`detail_original_chars`, `detail_reduced_fields` (when space permits), and
`detail_omitted_fields_count` identify lossy summaries. Missing entries in a
summary are not evidence that the activity did not occur.

This change adds no RPC, retries or additional simulations. It cannot recover
exception text or audit details already discarded by an older running service.
