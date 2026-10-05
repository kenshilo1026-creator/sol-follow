"""Normalized jsonParsed legacy/v0/v1, including expanded v0 keys.

v1 moves compute fees to transactionConfig; this decoder uses actual meta.fee
and parsed transfer amounts, never estimates payment from ComputeBudget data.
"""
from dataclasses import dataclass
from chain_common.primitives import pubkey


class Unsupported(ValueError):
    pass


@dataclass
class Tx:
    raw: dict

    def __post_init__(self):
        r = self.raw
        if r.get('version', 'legacy') not in ('legacy', 0, 1):
            raise Unsupported('unsupported-version')
        if not r.get('meta') or r['meta'].get('err') is not None:
            raise Unsupported('failed-or-missing-meta')
        if not isinstance(r.get('blockTime'), int) or r['blockTime'] <= 0:
            raise Unsupported('block-time-unavailable')
        self.slot, self.time = int(r['slot']), r['blockTime']
        msg = r['transaction']['message']
        self.signature = r['transaction']['signatures'][0]
        self.keys = [k['pubkey'] if isinstance(k, dict) else k for k in msg['accountKeys']]
        if len(self.keys) != len(set(self.keys)):
            raise Unsupported('duplicate-keys')
        if not all(isinstance(k, dict) for k in msg['accountKeys']):
            raise Unsupported('jsonParsed-required')
        self.signers = {k['pubkey'] for k in msg['accountKeys'] if k.get('signer')}
        self.meta = r['meta']
        if len(self.meta['preBalances']) != len(self.keys) or len(self.meta['postBalances']) != len(self.keys):
            raise Unsupported('missing-loaded-addresses')
        self.top = msg['instructions']
        self.inner = {int(g['index']): g['instructions'] for g in self.meta.get('innerInstructions') or []}

    def instructions(self):
        for i, ix in enumerate(self.top):
            yield str(i), ix
            for j, nested in enumerate(self.inner.get(i, [])):
                yield f'{i}.{j}', nested

    def accounts(self, ix):
        return [self.keys[a] if isinstance(a, int) else a for a in ix.get('accounts', [])]

    def delta(self, key):
        i = self.keys.index(key)
        return int(self.meta['postBalances'][i]) - int(self.meta['preBalances'][i])

    def tokens(self, phase):
        result = {}
        for row in self.meta.get(phase+'TokenBalances') or []:
            if row.get('owner'):
                idx = int(row['accountIndex'])
                if not 0 <= idx < len(self.keys):
                    raise Unsupported('token-account-index')
                result[self.keys[idx]] = (row['owner'], row['mint'], int(row['uiTokenAmount']['amount']))
        return result

    def owner_tokens(self, phase, owner, mint):
        return sum(n for o, m, n in self.tokens(phase).values() if o == owner and m == mint)
