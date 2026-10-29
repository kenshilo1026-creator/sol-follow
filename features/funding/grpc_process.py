"""Bounded binary IPC for an isolated, read-only gRPC receiver process."""
import asyncio
import base64
import json
import math
import os
import struct
import subprocess
import sys
import time
import grpc
from chain_common.yellowstone import geyser_pb2 as pb

HEADER=struct.Struct('!Idd')  # protobuf length, wall receipt time, monotonic receipt time
MAX_FRAME=16*1024*1024
MAX_CONTROL=8*1024*1024
QUEUE_MESSAGES=128
QUEUE_BYTES=32*1024*1024
MAX_QUEUE_AGE=2.0
EXIT_REASONS={71:'grpc-write-timeout',72:'grpc-idle-timeout',73:'grpc-ipc-backlog',
              74:'grpc-ended',75:'grpc-ipc-protocol',76:'grpc-receiver-error'}


class RemoteRpcError(grpc.RpcError):
    def __init__(self,code):self.status=code
    def code(self):return self.status


def child_environment():
    # No wallet, Telegram, provider or cloud credentials are inherited. Only
    # the stream key/endpoint are sent privately over stdin, never argv/logs.
    names={'PATH','SYSTEMROOT','WINDIR','SYSTEMDRIVE','TEMP','TMP','LANG','LC_ALL',
           'SSL_CERT_FILE','SSL_CERT_DIR','GRPC_DEFAULT_SSL_ROOTS_FILE_PATH'}
    return {k:v for k,v in os.environ.items() if k.upper() in names}


class ReceiverProcess:
    def __init__(self,config,*,command=None):
        self.config=config
        self.command=command or [sys.executable,'-B','-m','features.funding.grpc_receiver']
        self.process=None
        self.received_at=self.received_mono=None
        self.queue_ms=0.0

    async def __aenter__(self):
        self.process=await asyncio.create_subprocess_exec(*self.command,cwd=self.config.root,
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
            env=child_environment(),limit=64*1024,
            **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
        try:
            await self.control({'endpoint':self.config.grpc_endpoint,'key':self.config.alchemy_key})
        except BaseException:
            await self.close();raise
        return self

    async def control(self,value):
        raw=json.dumps(value,separators=(',',':')).encode()+b'\n'
        if len(raw)>MAX_CONTROL:raise RuntimeError('grpc-ipc-protocol')
        async with asyncio.timeout(5):
            self.process.stdin.write(raw)
            await self.process.stdin.drain()

    async def write(self,request):
        await self.control({'subscription':base64.b64encode(request.SerializeToString()).decode('ascii')})

    async def failed(self):
        try:
            async with asyncio.timeout(2):code=await self.process.wait()
        except TimeoutError:raise RuntimeError('grpc-receiver-error') from None
        if 80<=code<=96:
            status=next((s for s in grpc.StatusCode if s.value[0]==code-80),grpc.StatusCode.UNKNOWN)
            raise RemoteRpcError(status)
        raise RuntimeError(EXIT_REASONS.get(code,'grpc-receiver-error'))

    async def read(self):
        if self.process.returncode is not None:await self.failed()
        try:
            header=await self.process.stdout.readexactly(HEADER.size)
            size,wall,mono=HEADER.unpack(header)
            if (not 0<size<=MAX_FRAME or not math.isfinite(wall) or not math.isfinite(mono)
                or wall<=0 or mono<0):raise RuntimeError('grpc-ipc-protocol')
            data=await self.process.stdout.readexactly(size)
        except asyncio.IncompleteReadError:
            await self.failed()
        if self.process.returncode is not None:await self.failed()
        age=time.monotonic()-mono
        if age<0 or age>MAX_QUEUE_AGE:raise RuntimeError('grpc-ipc-backlog')
        self.received_at,self.received_mono=wall,mono
        self.queue_ms=age*1000
        try:return pb.SubscribeUpdate.FromString(data)
        except Exception:raise RuntimeError('grpc-ipc-protocol') from None

    def cancel(self):
        if self.process and self.process.returncode is None:
            try:self.process.terminate()
            except ProcessLookupError:pass

    async def close(self):
        if not self.process:return
        if self.process.stdin:self.process.stdin.close()
        self.cancel()
        try:
            async with asyncio.timeout(2):await self.process.communicate()
        except TimeoutError:
            if self.process.returncode is None:self.process.kill()
            await self.process.communicate()

    async def __aexit__(self,*args):await self.close()
