'use strict';
// Pinned SDKs build fresh unsigned instructions. No key loading or broadcast.
const BN = require('bn.js');
const assert = require('node:assert/strict');
const web3 = require('@solana/web3.js');
const spl = require('@solana/spl-token');
const pump = require('@pump-fun/pump-sdk');
const dlmmModule = require('@meteora-ag/dlmm');
const DLMM = dlmmModule.default || dlmmModule;
const DLMM_ID = new web3.PublicKey('LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo');
const MEMO = new web3.PublicKey('MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr');
const buyIdl = pump.pumpIdl.instructions.find(x => x.name === 'buy_v2');
const exactIdl = pump.pumpIdl.instructions.find(x => x.name === 'buy_exact_quote_in_v2');
const REASONS = new Set(['invalid-integer','invalid-u64','invalid-percent','dust-route',
  'unsupported-mint-extension','uninitialized-mint','unexpected-signer','transaction-too-large',
  'non-native-only','invalid-slot','invalid-tables','account-owner','token-program',
  'curve-graduated-or-quote-mismatch','dlmm-quote-mismatch','dlmm-token-program',
  'insufficient-sol-liquidity','partial-swap','rounding-exhausts-slippage','simulation-rejected']);

function integer(v) {
  if (!/^[0-9]+$/.test(String(v))) throw Error('invalid-integer');
  const n = new BN(String(v));
  if (n.isZero() || n.bitLength() > 64) throw Error('invalid-u64');
  return n;
}
function fraction(percent) {
  if (!/^\d+(\.\d+)?$/.test(String(percent))) throw Error('invalid-percent');
  const [whole, decimals = ''] = String(percent).split('.');
  const denominator = new BN(10).pow(new BN(decimals.length)).muln(100);
  const numerator = new BN(whole + decimals);
  if (numerator.isZero() || numerator.muln(5).gt(denominator)) throw Error('invalid-percent');
  return {numerator, denominator};
}
function minimums(quoteOut, expectedTokens, percent) {
  const {numerator:n, denominator:d} = fraction(percent);
  // Reserve half the end-to-end tolerance for the first leg. The final Pump
  // bound uses the FULL quote's token output, so tolerances never compound.
  const quoteIn = quoteOut.mul(d.muln(2).sub(n)).div(d.muln(2));
  const minOut = expectedTokens.mul(d.sub(n)).add(d.subn(1)).div(d);
  if (quoteIn.lten(0) || minOut.lten(0)) throw Error('dust-route');
  return {quoteIn, minOut};
}
function exactQuoteInstruction(ix, quoteIn, minOut) {
  assert.deepEqual(buyIdl.accounts, exactIdl.accounts);
  assert(ix.programId.equals(pump.PUMP_PROGRAM_ID));
  assert(ix.data.subarray(0,8).equals(Buffer.from(buyIdl.discriminator)));
  return new web3.TransactionInstruction({programId:ix.programId, keys:ix.keys,
    data:Buffer.concat([Buffer.from(exactIdl.discriminator),
                       quoteIn.toArrayLike(Buffer,'le',8),minOut.toArrayLike(Buffer,'le',8)])});
}
function safeMint(mint) {
  // SPCX has dormant hook, pausable, default-state and confidential-capability
  // extensions. Public transfers are supported while initialized/unpaused and
  // with no active hook. Confidential balances themselves are never used.
  const allowed = new Set([0,3,4,6,10,12,14,18,19,20,21,22,23,25,26]);
  if (spl.getExtensionTypes(mint.tlvData).some(t => !allowed.has(t)))
    throw Error('unsupported-mint-extension');
  const hook=spl.getTransferHook(mint);
  const state=spl.getDefaultAccountState(mint);
  if ((hook && !hook.programId.equals(web3.PublicKey.default))
      || spl.getPausableConfig(mint)?.paused
      || (state && state.state!==spl.AccountState.Initialized))
    throw Error('unsupported-mint-extension');
  if (!mint.isInitialized) throw Error('uninitialized-mint');
}
function compile(user, blockhash, instructions, tables) {
  const msg = new web3.TransactionMessage({payerKey:user, recentBlockhash:blockhash,
    instructions}).compileToV0Message(tables);
  if (msg.header.numRequiredSignatures !== 1) throw Error('unexpected-signer');
  const tx = new web3.VersionedTransaction(msg);
  if (tx.serialize().length > 1232) throw Error('transaction-too-large');
  return tx;
}

async function build(input, injectedConnection) {
  const user = new web3.PublicKey(input.wallet), mint = new web3.PublicKey(input.mint);
  const quoteMint = new web3.PublicKey(input.quoteMint), poolKey = new web3.PublicKey(input.pool);
  const tokenProgram = new web3.PublicKey(input.tokenProgram), quoteProgram = new web3.PublicKey(input.quoteProgram);
  if (quoteMint.equals(spl.NATIVE_MINT) || quoteMint.equals(web3.PublicKey.default)) throw Error('non-native-only');
  const amount = integer(input.amount);
  fraction(input.slippagePercent);
  if (!Number.isSafeInteger(input.minSlot) || input.minSlot < 0) throw Error('invalid-slot');
  if (!Array.isArray(input.lookupTables) || input.lookupTables.length > 8) throw Error('invalid-tables');
  const connection = injectedConnection || new web3.Connection(input.rpc, {
    commitment:'confirmed', disableRetryOnRateLimit:true,
    // Apply the signal slot to SDK account reads as well as our own reads.
    fetchMiddleware: (url, options, next) => {
      const body = JSON.parse(options.body);
      const index = {getAccountInfo:1,getMultipleAccounts:1,getProgramAccounts:1,
                     getLatestBlockhash:0,simulateTransaction:1}[body.method];
      if (index !== undefined) {
        body.params[index] = {...body.params[index], minContextSlot:input.minSlot};
        options.body = JSON.stringify(body);
      }
      next(url, options);
    }
  });
  const accounts = await connection.getMultipleAccountsInfo([
    mint,quoteMint,poolKey,pump.bondingCurvePda(mint),pump.GLOBAL_PDA,pump.PUMP_FEE_CONFIG_PDA]);
  const owners=[tokenProgram,quoteProgram,DLMM_ID,pump.PUMP_PROGRAM_ID,pump.PUMP_PROGRAM_ID,pump.PUMP_FEE_PROGRAM_ID];
  accounts.forEach((a,i) => {
    if (!a || a.executable || !a.owner.equals(owners[i])) throw Error('account-owner');
  });
  for (const program of [tokenProgram,quoteProgram])
    if (![spl.TOKEN_PROGRAM_ID,spl.TOKEN_2022_PROGRAM_ID].some(p=>p.equals(program))) throw Error('token-program');
  const baseMint = spl.unpackMint(mint,accounts[0],tokenProgram);
  const quoteMintInfo = spl.unpackMint(quoteMint,accounts[1],quoteProgram);
  safeMint(baseMint); safeMint(quoteMintInfo);
  const curve = pump.PUMP_SDK.decodeBondingCurve(accounts[3]);
  if (curve.complete || !curve.quoteMint.equals(quoteMint)) throw Error('curve-graduated-or-quote-mismatch');
  const global = pump.PUMP_SDK.decodeGlobal(accounts[4]);
  const feeConfig = pump.PUMP_SDK.decodeFeeConfig(accounts[5]);
  const pair = await DLMM.create(connection,poolKey);
  const swapForY = pair.lbPair.tokenXMint.equals(spl.NATIVE_MINT);
  if (!(swapForY ? pair.lbPair.tokenYMint : pair.lbPair.tokenXMint).equals(quoteMint)
      || !(swapForY ? pair.lbPair.tokenXMint : pair.lbPair.tokenYMint).equals(spl.NATIVE_MINT))
    throw Error('dlmm-quote-mismatch');
  const nativeReserve = swapForY ? pair.tokenX : pair.tokenY;
  const quoteReserve = swapForY ? pair.tokenY : pair.tokenX;
  if (!nativeReserve.owner.equals(spl.TOKEN_PROGRAM_ID) || !quoteReserve.owner.equals(quoteProgram)) throw Error('dlmm-token-program');
  if (nativeReserve.amount < BigInt(input.minLiquidity)) throw Error('insufficient-sol-liquidity');
  const bins = await pair.getBinArrayForSwap(swapForY);
  const quote = pair.swapQuote(amount,swapForY,new BN(0),bins,false);
  if (!quote.consumedInAmount.eq(amount)) throw Error('partial-swap');
  const pumpQuote = q => pump.getBuyTokenAmountFromSolAmount({global,feeConfig,
    mintSupply:new BN(baseMint.supply.toString()),bondingCurve:curve,amount:q});
  const expected = pumpQuote(quote.outAmount);
  const {quoteIn,minOut} = minimums(quote.outAmount,expected,input.slippagePercent);
  if (pumpQuote(quoteIn).lt(minOut)) throw Error('rounding-exhausts-slippage');
  const nativeAta = spl.getAssociatedTokenAddressSync(spl.NATIVE_MINT,user);
  const quoteAta = spl.getAssociatedTokenAddressSync(quoteMint,user,false,quoteProgram);
  const targetAta = spl.getAssociatedTokenAddressSync(mint,user,false,tokenProgram);
  const [nativeExisting,targetExisting] = await connection.getMultipleAccountsInfo([nativeAta,targetAta]);
  const ixs = [
    spl.createAssociatedTokenAccountIdempotentInstruction(user,nativeAta,user,spl.NATIVE_MINT),
    spl.createAssociatedTokenAccountIdempotentInstruction(user,quoteAta,user,quoteMint,quoteProgram),
    web3.SystemProgram.transfer({fromPubkey:user,toPubkey:nativeAta,lamports:BigInt(amount.toString())}),
    spl.createSyncNativeInstruction(nativeAta),
  ];
  // Direct SDK instruction construction avoids a separate simulation of the
  // first hop and lets us simulate the actual atomic transaction once.
  ixs.push(await pair.program.methods.swap2(amount,quoteIn,{slices:[]}).accountsPartial({
    lbPair:poolKey,reserveX:pair.lbPair.reserveX,reserveY:pair.lbPair.reserveY,
    tokenXMint:pair.lbPair.tokenXMint,tokenYMint:pair.lbPair.tokenYMint,
    tokenXProgram:pair.tokenX.owner,tokenYProgram:pair.tokenY.owner,
    user,userTokenIn:nativeAta,userTokenOut:quoteAta,
    binArrayBitmapExtension:pair.binArrayBitmapExtension?.publicKey || null,
    oracle:pair.lbPair.oracle,hostFeeIn:null,memoProgram:MEMO,
  }).remainingAccounts(quote.binArraysPubkey.map(pubkey=>({pubkey,isSigner:false,isWritable:true}))).instruction());
  const pumpIxs = await pump.PUMP_SDK.buyV2Instructions({global,
    bondingCurveAccountInfo:accounts[3],bondingCurve:curve,associatedUserAccountInfo:targetExisting,
    mint,user,amount:minOut,quoteAmount:quoteIn,slippage:0,tokenProgram,quoteTokenProgram:quoteProgram});
  ixs.push(...pumpIxs.map(ix=>ix.programId.equals(pump.PUMP_PROGRAM_ID)
    ? exactQuoteInstruction(ix,quoteIn,minOut) : ix));
  // Preserve a pre-existing wrapped SOL account and its balance.
  if (!nativeExisting) ixs.push(spl.createCloseAccountInstruction(nativeAta,user,user));
  const tables = (await Promise.all(input.lookupTables.map(async key =>
    (await connection.getAddressLookupTable(new web3.PublicKey(key))).value))).filter(Boolean);
  let block = await connection.getLatestBlockhash('confirmed');
  let tx = compile(user,block.blockhash,[web3.ComputeBudgetProgram.setComputeUnitLimit({units:1400000}),...ixs],tables);
  const simulated = await connection.simulateTransaction(tx,{sigVerify:false,commitment:'confirmed'});
  if (simulated.value.err || !simulated.value.unitsConsumed) throw Error('simulation-rejected');
  const units = Math.min(1400000,Math.ceil(simulated.value.unitsConsumed*1.2));
  block = await connection.getLatestBlockhash('confirmed');
  tx = compile(user,block.blockhash,[web3.ComputeBudgetProgram.setComputeUnitLimit({units}),...ixs],tables);
  return {transaction:Buffer.from(tx.serialize()).toString('base64'),lastHeight:block.lastValidBlockHeight,
    wallet:user.toBase58(),mint:mint.toBase58(),targetAta:targetAta.toBase58(),
    amount:amount.toString(),quotedOut:expected.toString(),minOut:minOut.toString(),
    quoteIn:quoteIn.toString(),quoteOut:quote.outAmount.toString(),units};
}
module.exports = {build,minimums,exactQuoteInstruction,safeMint,compile};
if (require.main === module) {
  let input='';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data',chunk=>{input+=chunk;if(input.length>100000)process.exit(1);});
  process.stdin.on('end',async()=>{
    try {process.stdout.write(JSON.stringify(await build(JSON.parse(input))));}
    catch (error) {process.stdout.write(JSON.stringify({error:REASONS.has(error.message)
      ? error.message : 'sdk-or-rpc-failed'}));process.exitCode=1;}
  });
}
