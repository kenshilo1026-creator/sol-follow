"""Simulate, durably sign, submit once, then reconcile by signature."""
import asyncio
import base64
import json
import time
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from chain_common.transaction import Tx
from trade_execution.builder import build

MARKER = 'atomic-pump-buy-v1'


class Executor:
    def __init__(self, config, store, rpc, notices, priority, builder=build):
        self.config,self.store,self.rpc,self.notices,self.priority=config,store,rpc,notices,priority
        self.builder=builder

    def fresh(self, route):
        return time.time()-route.trade.time <= self.config.signal_age

    async def buy(self, oid, route):
        row=self.store.order(oid)
        if row['state']!='reserved' or row['mode']!=self.config.mode:
            return
        self.store.update_order(oid,reason=MARKER)
        signed=False
        self.priority.active+=1
        try:
            if self.store.rows('trading',"SELECT 1 FROM positions WHERE mint=? AND mode=? AND amount!='0'",(row['mint'],row['mode'])):
                raise ValueError('position-already-exists')
            if not self.fresh(route):
                raise ValueError('signal-expired')
            # A dry run may simulate with the observed funded address when no
            # local public wallet is configured. It never reads that wallet's key.
            wallet=self.config.wallet_address or route.trade.wallet
            result=await self.builder(self.config,route,wallet)
            if not self.fresh(route):
                raise ValueError('signal-expired-after-build')
            wire=base64.b64decode(result['transaction'],validate=True)
            tx=VersionedTransaction.from_bytes(wire)
            if (len(wire)>1232 or tx.message.header.num_required_signatures!=1
                or str(tx.message.account_keys[0])!=wallet or result['wallet']!=wallet
                or result['mint']!=row['mint'] or int(result['amount'])!=int(row['amount'])
                or not 0<int(result['minOut'])<=int(result['quotedOut'])):
                raise ValueError('invalid-built-transaction')
            self.store.update_order(oid,min_out=result['minOut'],quoted_out=result['quotedOut'])
            simulation=await self.rpc.call('simulateTransaction',[result['transaction'],
                {'encoding':'base64','sigVerify':False,'commitment':'confirmed','minContextSlot':route.trade.slot}])
            if simulation['value']['err'] is not None:
                raise ValueError('simulation-rejected')
            if not self.fresh(route):
                raise ValueError('signal-expired-after-simulation')
            if self.config.dry_run:
                with self.store.db('trading') as c:
                    c.execute('BEGIN IMMEDIATE')
                    c.execute("UPDATE orders SET state='dry-simulated',updated=? WHERE id=?",(time.time(),oid))
                    c.execute("UPDATE signals SET state='dry-simulated' WHERE order_id=?",(oid,))
                    c.commit()
                self.notices.emit('buy dry simulation passed',{'order':oid,'mint':row['mint'],
                    'min_out':result['minOut'],'quote_spend':result['quoteIn'],'simulation_wallet':wallet},alert=True)
                return
            # Local Solana CLI JSON keypair. No key material goes through Node,
            # provider request bodies, logs or the database.
            keydata=json.loads(self.config.wallet_file.read_text(encoding='utf-8'))
            keypair=Keypair.from_bytes(bytes(keydata))
            if str(keypair.pubkey())!=wallet:
                raise ValueError('wallet-keypair-mismatch')
            if not self.fresh(route):
                raise ValueError('signal-expired-before-signing')
            tx=VersionedTransaction(tx.message,[keypair])
            raw=base64.b64encode(bytes(tx)).decode()
            signature=str(tx.signatures[0])
            # FULL durability before sending, including crash between send and
            # response. An ambiguous send never releases the mint reservation.
            with self.store.db('trading') as c:
                c.execute('PRAGMA synchronous=FULL')
                c.execute('BEGIN IMMEDIATE')
                c.execute("UPDATE orders SET state='signed',raw=?,signature=?,last_height=?,updated=? WHERE id=?",
                          (raw,signature,int(result['lastHeight']),time.time(),oid))
                c.commit()
            signed=True
            response=await self.rpc.call('sendTransaction',[raw,{'encoding':'base64','skipPreflight':False,
                'preflightCommitment':'confirmed','maxRetries':0,'minContextSlot':route.trade.slot}])
            if response!=signature:
                raise ValueError('send-signature-mismatch')
            self.store.update_order(oid,state='submitted')
            self.notices.emit('buy submitted',{'order':oid,'signature':signature,'mint':row['mint']},alert=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if signed:
                self.store.update_order(oid,state='unknown')
            else:
                self.store.fail_order(oid,'build-or-simulation-rejected:'+type(exc).__name__)
            self.notices.emit('buy deferred' if signed else 'buy rejected',
                {'order':oid,'type':type(exc).__name__,'signed':signed},alert=True)
        finally:
            self.priority.active-=1

    def recover_unsigned(self):
        # An interrupted build has no signed bytes to broadcast. Never turn an
        # old reservation into a fresh buy after a process restart.
        for row in self.store.rows('trading',"SELECT id FROM orders WHERE state='reserved' AND reason=?",(MARKER,)):
            self.store.fail_order(row['id'],'interrupted-before-signing')

    async def reconcile_once(self):
        rows=self.store.rows('trading',"SELECT * FROM orders WHERE reason=? AND mode='live' "
            "AND state IN ('signed','submitted','unknown','confirmed')",(MARKER,))
        if not rows:
            return
        result=await self.rpc.call('getSignatureStatuses',[[r['signature'] for r in rows],{'searchTransactionHistory':True}])
        for row,status in zip(rows,result['value']):
            if status and status.get('confirmationStatus')=='finalized':
                if status.get('err') is not None:
                    self.store.fail_order(row['id'],'finalized-transaction-error')
                    self.notices.emit('buy failed on chain',{'order':row['id'],'signature':row['signature']},alert=True)
                    continue
                raw=await self.rpc.transaction(row['signature'],commitment='finalized')
                if raw is None:
                    continue
                tx=Tx(raw)
                signed=VersionedTransaction.from_bytes(base64.b64decode(row['raw']))
                wallet=str(signed.message.account_keys[0])
                if tx.signature!=row['signature'] or wallet not in tx.signers:
                    raise ValueError('fill-transaction-mismatch')
                tokens=tx.owner_tokens('post',wallet,row['mint'])-tx.owner_tokens('pre',wallet,row['mint'])
                if tokens<int(row['min_out']):
                    self.notices.emit('buy fill requires review',{'order':row['id']},alert=True,key='fill-review:'+row['id'])
                    continue
                if self.store.fill(row['id'],tokens,int(row['amount'])):
                    self.notices.emit('buy finalized',{'order':row['id'],'signature':row['signature'],'tokens':tokens},alert=True)
            elif status and status.get('err') is None and status.get('confirmationStatus')=='confirmed':
                self.store.update_order(row['id'],state='confirmed')
            elif not status:
                # A pruned/lagging RPC is not proof a buy never landed. Keep the
                # signature and permanent reservation; do not re-sign/rebuy.
                height=await self.rpc.call('getBlockHeight',[{'commitment':'finalized'}])
                if height>row['last_height']:
                    self.store.update_order(row['id'],state='unknown')
                    self.notices.emit('buy status unknown after expiry; reservation retained',
                        {'order':row['id'],'signature':row['signature']},alert=True,key='buy-unknown:'+row['id'],interval=1800)

    async def reconcile(self):
        while True:
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.notices.emit('buy reconciliation deferred',{'type':type(exc).__name__},alert=True,key='buy-reconcile')
            await asyncio.sleep(2)
