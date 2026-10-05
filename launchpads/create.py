"""Inspect successful normalized transactions for supported creation instructions."""
from dataclasses import dataclass
from chain_common.primitives import b58decode
from chain_common.transaction import Unsupported
from launchpads import pump_fun,stonk
from launchpads import create_pump,create_stonk

SUPPORTED=('pump_create','pump_create_v2','stonk_initialize','stonk_initialize_v2','stonk_initialize_with_token_2022')


@dataclass
class Inspection:
    creates: list
    rejected: list


def inspect(tx):
    creates,rejected=[],[]
    for path,ix in tx.instructions():
        decoder={pump_fun.PROGRAM:create_pump,stonk.PROGRAM:create_stonk}.get(ix.get('programId'))
        if decoder is None:continue
        try:
            data=b58decode(ix.get('data',''))
            if data[:8] not in decoder.DISCRIMINATORS:continue
            result=decoder.decode(tx,path,ix,data)
            if result is not None:creates.append(result)
        except (Unsupported,ValueError,KeyError,IndexError,TypeError) as exc:
            reason=str(exc) if isinstance(exc,Unsupported) else 'malformed-create-instruction'
            rejected.append({'instruction_path':path,'program':ix.get('programId'),'reason':reason})
    return Inspection(creates,rejected)


def decode(tx):return inspect(tx).creates
