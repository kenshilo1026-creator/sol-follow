"""Shared HTTP RPC client with provider-directed cooldown."""
import asyncio
import time
import aiohttp


class RpcError(RuntimeError):
    pass


class Priority:
    def __init__(self):
        self.active = 0
        self.cooldown = {}


class Rpc:
    def __init__(self, session, url, priority):
        self.session, self.url, self.priority = session, url, priority
        self.calls = self.errors = self.limited = 0

    async def call(self, method, params):
        if time.monotonic() < self.priority.cooldown.get(self.url, 0):
            raise RpcError('endpoint-cooling')
        self.calls += 1
        try:
            async with self.session.post(self.url, json={'jsonrpc':'2.0','id':1,'method':method,'params':params},
                                         timeout=aiohttp.ClientTimeout(total=8)) as response:
                if response.status == 429:
                    self.limited += 1
                    try:
                        delay = max(1, min(120, float(response.headers.get('Retry-After', '10'))))
                    except ValueError:
                        delay = 10
                    self.priority.cooldown[self.url] = time.monotonic()+delay
                    raise RpcError('HTTP-429')
                if response.status != 200:
                    raise RpcError(f'HTTP-{response.status}')
                # Do not put provider response bodies / URLs / credentials in logs.
                body = await response.json()
                if 'error' in body:
                    code = body['error'].get('code', 'unknown')
                    if code in (-32005, 429):
                        self.priority.cooldown[self.url] = time.monotonic()+10
                    raise RpcError(f'{method}:RPC-{code}')
                if 'result' not in body:
                    raise RpcError('RPC-missing-result')
                return body['result']
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            self.errors += 1
            raise RpcError(f'{method}:{type(exc).__name__}') from None

    async def transaction(self, signature, commitment='confirmed'):
        return await self.call('getTransaction', [signature, {'encoding':'jsonParsed',
                               'commitment':commitment,'maxSupportedTransactionVersion':1}])


class ReadCache:
    def __init__(self, rpc):
        self.rpc = rpc
        self.blockhash = None
        self.balance = None
        self.rent = None
        self.updated = 0.0

    async def refresh(self, wallet):
        block = await self.rpc.call('getLatestBlockhash', [{'commitment':'confirmed'}])
        balance = (await self.rpc.call('getBalance', [wallet, {'commitment':'confirmed'}]))['value'] if wallet else None
        rent = self.rent
        if rent is None:
            rent = await self.rpc.call('getMinimumBalanceForRentExemption', [165, {'commitment':'confirmed'}])
        self.blockhash, self.balance, self.rent = block, balance, rent
        self.updated = time.monotonic()

    def fresh(self):
        return self.blockhash is not None and time.monotonic()-self.updated < 5
