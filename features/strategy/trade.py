"""Venue-independent trade event for the strategy and storage layer."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Trade:
    event: str
    signature: str
    slot: int
    time: int
    wallet: str
    mint: str
    side: str
    quote: int
    tokens: int
    remaining: int
    pool: str
