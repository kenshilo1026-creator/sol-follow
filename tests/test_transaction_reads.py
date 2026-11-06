"""Receipt reuse stays job-local and cannot strengthen commitment evidence."""
import copy
import pytest
from features.funding.work import TransactionReads
from tests.helpers import address, transaction


def receipt(sig):
    return transaction([address()], [], [], sig=sig, slot=100, when=1000)


class Reader:
    def __init__(self):
        self.calls = []
        self.value = receipt('old')

    async def transaction(self, sig, commitment='confirmed'):
        self.calls.append((sig, commitment))
        if isinstance(self.value, Exception):
            raise self.value
        return copy.deepcopy(self.value)


@pytest.mark.asyncio
async def test_repeated_history_is_reused_without_mutable_aliases():
    rpc = Reader()
    stats = {}
    reader = TransactionReads(rpc, receipt('deposit'), stats=stats)
    first = await reader.transaction('old')
    first['meta']['err'] = 'caller mutation'
    assert (await reader.transaction('old'))['meta']['err'] is None
    assert rpc.calls == [('old', 'confirmed')]
    assert stats == {'history_requested': 1, 'history_reused': 1}
    await reader.transaction('old', 'finalized')
    assert rpc.calls[-1] == ('old', 'finalized')
    await TransactionReads(rpc, receipt('deposit')).transaction('old')
    assert len(rpc.calls) == 3  # No reuse across jobs or retries.


@pytest.mark.asyncio
async def test_expiry_and_capacity_bound_reuse():
    rpc = Reader()
    now = [0]
    reader = TransactionReads(rpc, receipt('deposit'), clock=lambda: now[0])
    reader.MAX_ENTRIES = 1
    await reader.transaction('old')
    now[0] = reader.TTL
    await reader.transaction('old')
    assert len(rpc.calls) == 2
    rpc.value = receipt('second')
    await reader.transaction('second')
    assert list(reader.receipts) == ['second']
    reader.MAX_BYTES = 1
    rpc.value = receipt('oversize')
    await reader.transaction('oversize')
    await reader.transaction('oversize')
    assert 'oversize' not in reader.receipts
    assert reader.bytes == sum(row[2] for row in reader.receipts.values())


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['null', 'mismatch', 'processed', 'error'])
async def test_unusable_or_processed_responses_are_not_cached(kind):
    rpc = Reader()
    rpc.value = {'null': None, 'mismatch': receipt('different'),
                 'processed': {**receipt('old'), '_stream': {'commitment': 'processed'}},
                 'error': RuntimeError('offline')}[kind]
    reader = TransactionReads(rpc, receipt('deposit'))
    for _ in range(2):
        if kind == 'error':
            with pytest.raises(RuntimeError):
                await reader.transaction('old')
        else:
            await reader.transaction('old')
    assert len(rpc.calls) == 2 and not reader.receipts


@pytest.mark.asyncio
async def test_rpc_method_counts_are_physical_attempts_without_secrets():
    from chain_common.rpc import Rpc, Priority, RpcError
    from tests.test_rpc_alerts import Session, Reply
    rpc = Rpc(Session(Reply()), 'https://example.invalid/secret', Priority(), source='alchemy-history')
    for _ in range(2):
        with pytest.raises(RpcError):
            await rpc.call('getTransaction', ['private-signature'])
    snapshot = rpc.snapshot()
    assert snapshot['methods'] == {'getTransaction': 1}  # Cooldown skip is not a request.
    assert snapshot['source'] == 'alchemy-history'
    assert 'secret' not in str(snapshot) and 'private-signature' not in str(snapshot)
