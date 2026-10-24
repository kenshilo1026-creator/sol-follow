"""Public-first stream head read with recovery independent of funding failures."""
from chain_common.rpc import RpcError
from features.funding.rpc import FundingRpc


class StreamHeadRpc(FundingRpc):
    METHODS = {'getSlot'}

    @staticmethod
    def valid(method, result, params):
        return method=='getSlot' and type(result) is int and result>=0

    def unavailable(self, method):
        # HotlistFeed reports the failed startup/reconnect once, after both reads.
        return RpcError('stream-head-rpc-unavailable')
