# Disabled factory registrations

Additional factories are registered in `launchpads/factories.json`. Lookup uses both program and platform; sharing the LaunchLab program does not make a token Stonk.

The HAPPA sample uses LaunchLab program `LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj` and platform `FTFNRRsEjyE175KkQFco4txrDpcsh5suZvtPhVFhmZ1C`. The supplied successful transaction initializes mint `5RugYLCZZogHJFT7YXNVXNFjgYxStDPRoQZgsiSv2CrR` in slot 464847892. A read-only account lookup also verified the platform's owner and PlatformConfig discriminator and the pool's platform/mint fields. The platform brand has not been established, so its registry name uses its address prefix.

`enable=false` is a trading switch, not a create-observation switch. This general rule applies to every registered factory with a verified create decoder, including Pump and Stonk if disabled in the same registry:

- Fully validate the create ABI, platform, PDA accounts and successful transaction evidence. Attribute consumption only to the signing launcher (`user`), not an arbitrary metadata creator.
- Consume the wallet's qualifying prior funding and remove its corresponding hotlist entry, even when the buy funding-age threshold has not been reached.
- Persist `hotlist_removals.reason=dev-create-disabled` and a `factory-disabled` dev audit with `eligibility_consumed`. Do not reserve, build or submit a buy.
- Processed and confirmed backup feeds share this path. Retries and restart preserve the tombstone; newer deposits are not removed by an older create. A proven invalid transaction/dead slot restores the corresponding eligibility using the existing rollback mechanism.
- Disabled creates also count as launch history during funding qualification. Failed/malformed/unverified transactions do not consume eligibility.

HAPPA remains disabled. LaunchLab initialize decoding is now shared for registered platforms, while trade execution remains restricted to the implemented Pump/Stonk adapters. Switching HAPPA to true alone does not implement its origin proof, buy/sell adapter or SDK validation. Unknown programs/ABIs still require a verified decoder before their creates can be attributed; registration alone is not evidence.

Factory configuration loads at process startup; restart the service after changing it.
