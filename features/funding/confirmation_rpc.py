"""Public-first status checks for funding finality and processed signals."""
from chain_common.public_rpc import report_confirmation_unavailable
from chain_common.rpc import RpcError
from features.funding.rpc import FundingRpc


class ConfirmationUnavailable(RpcError):
    """Both providers unavailable; the combined notification is already handled."""


class ConfirmationRpc(FundingRpc):
    METHODS = {'getSignatureStatuses'}

    @staticmethod
    def valid(method, result, params):
        if method != 'getSignatureStatuses' or not isinstance(result, dict):
            return False
        context, rows = result.get('context'), result.get('value')
        if (not isinstance(context, dict) or type(context.get('slot')) is not int
                or context['slot'] < 0 or not isinstance(rows, list)
                or len(rows) != len(params[0])):
            return False
        for row in rows:
            # A missing signature is valid unknown evidence, never confirmation.
            if row is None:
                continue
            if (not isinstance(row, dict) or type(row.get('slot')) is not int
                    or not 0 <= row['slot'] <= context['slot'] or 'err' not in row
                    or row['err'] is not None and not isinstance(row['err'], (str, dict))
                    or row.get('confirmationStatus') not in ('processed', 'confirmed', 'finalized')):
                return False
        return True

    def unavailable(self, method):
        if self.alchemy is not None:
            now=self.clock()
            outage=self.outages.setdefault(method,{'since':now,'notified':False})
            alert=now-outage['since']>60 and not outage['notified']
            report_confirmation_unavailable(method,alert=alert)
            if alert:outage['notified']=True
            return ConfirmationUnavailable('confirmation-rpc-unavailable')
        return RpcError('confirmation-rpc-unavailable')
