# Pump create / V3 snapshot compatibility

The fast path accepts successful Pump `Create`/`CreateV2` transactions containing
`BuyV3`, `BuyExactQuoteInV3`, `SellV3`, and `ExtendAccount` of the same curve.
It preserves program-attributed event order and verifies the extension instruction
accounts/discriminator. Unknown actions still fall back to account reads.

`pump-create-events.cjs` bounds the event layout, including CreateEvent depth and
TradeEvent creator_fee_unclaimed. Every buy/sell must reconcile token and quote
changes against the preceding event. SOL aliases must agree with quote fields.
Reserves are the final event's virtual/real reserves, not the account's gross
lamports/token balance: retained protocol/creator fees and rent are excluded.
The projected 125-byte curve is a quote-only view for the existing builder; it is
not a full modern account and never enters the shared account cache.

The real fixture (create signature `4GWEjSyJUoKMhhHRX1QbxhFH3cjrxnBwHD6px1iFdvNs3C1pAhMr5LXMyRX9nDHHF1pJprR2Df76esohUn1pKpJD`,
slot 465949034) has another eight zero bytes after the published TradeEvent fields.
Those bytes remain opaque. This exact shape is accepted only with receipt balance
corroboration: final base vault tokens and quote balance must equal the projected
reserves plus known unsold inventory / retained fees. For SOL, independently
identified creation/extension rent is subtracted. Missing proof, nonzero/other
unknown tails or inconsistent balances disable projection. Compiled System
CreateAccount from processed gRPC is supported alongside parsed RPC receipts.
CPI launches whose rent cannot be independently recovered use the normal fallback
for this opaque-tail variant. No extra RPC is issued to obtain this proof.

For this fixture:
- virtual token reserves: 1059053615030649
- virtual quote reserves: 30395061727
- real token reserves: 779153615030649
- real quote reserves: 395061727
- retained fees: 3061730; rent: 1493520
- curve lamports: 399616977 = real quote + retained fees + rent

Completion / synthetic migration events, unsupported mint flags and unknown
mutations do not produce a tradable projection. The existing source-slot simulation
and mint verification still run. Outgoing buys retain their existing instruction
format and fee/slippage settings. Non-SOL projections preserve quote-token units;
the route's quote conversion cache is unchanged.

Offline tests replay the real receipt and protobuf-normalized form, exercise old
Pump/Stonk cases, broken proofs/layouts, multiple reserve transitions and non-SOL
quote units. A build with populated common settings makes zero account RPC calls,
including when the settings survive a socket disconnect. This is not a mainnet
latency measurement or a same-slot guarantee.

## Existing 271 ms timing

The supplied VPS audit has 270.618 ms from create receipt to submit start:
32.098 ms before executor timing, 225.772 ms build, and 12.748 ms from build end
to submit start. Inside build, account reading consumed 158.644 ms and simulation
30.102 ms; remaining build/IPC work was about 37.026 ms. The 96.170 ms submit ACK
wait occurs after this interval. Removing the account read targets the largest
known component; subtracting it yields about 112 ms as arithmetic, not a measured
new latency. Deployment audit must verify cache availability and the new timing.

Sources:
- https://github.com/pump-fun/pump-public-docs/blob/main/idl/pump.json
- https://github.com/pump-fun/pump-public-docs/blob/main/docs/instructions/TRADE_V3.md
- https://github.com/pump-fun/pump-public-docs/blob/main/docs/instructions/SWEEP_FEES.md
- https://github.com/pump-fun/pump-public-docs/blob/main/docs/SYNTHETIC_MIGRATION.md
