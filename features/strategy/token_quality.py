"""Bounded off-chain social-link gate; never calls Solana RPC."""
import asyncio
import base64
from collections import OrderedDict
import ipaddress
import json
import time
from urllib.parse import urlsplit, urljoin, unquote_to_bytes

import aiohttp

MAX_BYTES = 131072
SOCIAL_HOSTS = {'x.com', 'twitter.com', 't.me', 'telegram.me', 'discord.gg',
                'discord.com', 'discordapp.com', 'instagram.com', 'facebook.com',
                'youtube.com', 'youtu.be', 'reddit.com', 'tiktok.com', 'warpcast.com'}
FIELDS = ('twitter','x','telegram','discord','instagram','facebook','youtube',
          'reddit','tiktok','warpcast','website','url')


def social_links(meta):
    """Inspect declared links, not arbitrary strings or the metadata URI itself."""
    if not isinstance(meta,dict):return []
    values=[]
    for block in (meta,meta.get('extensions'),meta.get('socials'),meta.get('links'),
                  (meta.get('properties') or {}).get('links') if isinstance(meta.get('properties'),dict) else None):
        if isinstance(block,dict):values.extend(block.get(k) for k in FIELDS)
        elif isinstance(block,list):
            values.extend(x.get('url') if isinstance(x,dict) else x for x in block[:32])
    found=set()
    for value in values:
        if not isinstance(value,str) or len(value)>2048:continue
        try:
            u=urlsplit(value.strip());host=(u.hostname or '').lower().removeprefix('www.')
            path=u.path.strip('/')
            if (u.scheme not in ('http','https') or u.username or u.password or u.port not in (None,80,443)
                    or host not in SOCIAL_HOSTS or not path or any(c.isspace() for c in value)):continue
            if path.split('/')[0].lower() in ('intent','share','search','home','login','signup','sharer.php'):continue
            if host in ('discord.com','discordapp.com') and not path.startswith('invite/'):continue
            found.add(host)
        except ValueError:continue
    return sorted(found)


def public_url(uri):
    if not isinstance(uri,str) or not uri or len(uri)>4096:raise ValueError('metadata-uri-invalid')
    if uri.startswith('ipfs://'):
        path=uri[7:].removeprefix('ipfs/')
        uri='https://ipfs.io/ipfs/'+path
    elif uri.startswith('ar://'):uri='https://arweave.net/'+uri[5:]
    u=urlsplit(uri)
    if (u.scheme not in ('https','http') or not u.hostname or u.username or u.password
            or u.port not in (None,80,443) or any(c.isspace() for c in uri)):
        raise ValueError('metadata-uri-invalid')
    try:address=ipaddress.ip_address(u.hostname)
    except ValueError:address=None
    if address and (not address.is_global or address.is_multicast):raise ValueError('metadata-uri-invalid')
    return uri


class PublicResolver(aiohttp.abc.AbstractResolver):
    def __init__(self):self.delegate=aiohttp.resolver.DefaultResolver()
    async def resolve(self,host,port=0,family=0):
        rows=await self.delegate.resolve(host,port,family)
        if not rows or any(not ipaddress.ip_address(r['host']).is_global
                           or ipaddress.ip_address(r['host']).is_multicast for r in rows):
            raise OSError('metadata-address-not-public')
        return rows
    async def close(self):await self.delegate.close()


async def fetch_metadata(uri):
    if uri.startswith('data:application/json'):
        header,body=uri.split(',',1)
        if header not in ('data:application/json','data:application/json;base64'):
            raise ValueError('metadata-invalid')
        raw=base64.b64decode(body,validate=True) if header.endswith(';base64') else unquote_to_bytes(body)
        if len(raw)>MAX_BYTES:raise ValueError('metadata-too-large')
        return json.loads(raw)
    url=public_url(uri)
    resolver=PublicResolver()
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=resolver),
                trust_env=False,headers={'Accept':'application/json'}) as session:
            for _ in range(3):
                async with session.get(url,allow_redirects=False) as response:
                    if response.status in (301,302,303,307,308):
                        url=public_url(urljoin(url,response.headers.get('Location','')))
                        continue
                    if response.status!=200:raise ValueError('metadata-http-failed')
                    body=bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        body.extend(chunk)
                        if len(body)>MAX_BYTES:raise ValueError('metadata-too-large')
                    return json.loads(body)
            raise ValueError('metadata-redirect-limit')
    finally:await resolver.close()


class TokenQuality:
    def __init__(self,timeout_ms=400,fetch=fetch_metadata):
        self.timeout_ms,self.fetch=timeout_ms,fetch
        self.cache=OrderedDict()

    async def check(self,uri,deadline):
        began=time.monotonic()
        def result(allowed,reason,links=(),cached=False):
            return dict(allowed=allowed,reason=reason,social_hosts=list(links),cache_hit=cached,
                        elapsed_ms=round((time.monotonic()-began)*1000,3))
        if not isinstance(uri,str) or not uri or len(uri)>4096:
            return result(False,'token-metadata-unavailable')
        cached=self.cache.get(uri)
        if cached and time.monotonic()-cached[0]<60:
            self.cache.move_to_end(uri)
            return result(bool(cached[1]),'token-social-link-found' if cached[1] else 'token-social-link-missing',cached[1],True)
        budget=min(self.timeout_ms/1000,deadline-time.time())
        if budget<=0:return result(False,'token-metadata-timeout')
        try:
            async with asyncio.timeout(budget):meta=await self.fetch(uri)
            if not isinstance(meta,dict):return result(False,'token-metadata-unavailable')
            links=social_links(meta)
        except TimeoutError:return result(False,'token-metadata-timeout')
        except (ValueError,OSError,aiohttp.ClientError,RecursionError):
            return result(False,'token-metadata-unavailable')
        self.cache[uri]=(time.monotonic(),links);self.cache.move_to_end(uri)
        while len(self.cache)>256:self.cache.popitem(last=False)
        return result(bool(links),'token-social-link-found' if links else 'token-social-link-missing',links)
