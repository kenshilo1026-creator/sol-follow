"""Known additional LaunchLab platforms; registration does not grant execution support."""
import json
from pathlib import Path
from chain_common.primitives import pubkey


def _load():
    body=json.loads(Path(__file__).with_name('factories.json').read_text(encoding='utf-8'))
    if body.get('version')!=1:raise ValueError('factory-registry-version')
    result={}
    for row in body['factories']:
        pubkey(row['program'])
        if row['platform']:pubkey(row['platform'])
        if type(row.get('enable')) is not bool:raise ValueError('factory-enable-must-be-boolean')
        key=(row['program'],row['platform'])
        if key in result:raise ValueError('duplicate-factory')
        result[key]=row
    return result


FACTORIES=_load()


def lookup(program,platform):
    return FACTORIES.get((program,platform))


def enabled(item):
    # Names are assigned by verified decoders, never token metadata.
    return all(row['enable'] for row in FACTORIES.values()
               if row['program']==item.program and row['name']==item.launchpad)
