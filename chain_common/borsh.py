"""Bounded Borsh primitives for instruction decoding, with strict EOF checks."""
import struct
from solders.pubkey import Pubkey
from chain_common.transaction import Unsupported


class Reader:
    def __init__(self, data):
        if not isinstance(data, bytes) or len(data)>8192:
            raise Unsupported('instruction-data-size')
        self.data,self.offset=data,0

    @property
    def remaining(self):
        return len(self.data)-self.offset

    def take(self, size):
        if size<0 or size>self.remaining:
            raise Unsupported('truncated-instruction-data')
        result=self.data[self.offset:self.offset+size]
        self.offset+=size
        return result

    def number(self, fmt):
        return struct.unpack('<'+fmt,self.take(struct.calcsize('<'+fmt)))[0]

    def u8(self):return self.number('B')
    def u16(self):return self.number('H')
    def u64(self):return self.number('Q')

    def boolean(self):
        result=self.u8()
        if result not in (0,1):raise Unsupported('invalid-bool')
        return bool(result)

    def string(self, limit):
        size=self.number('I')
        if size>limit:raise Unsupported('metadata-string-size')
        try:return self.take(size).decode('utf-8',errors='strict')
        except UnicodeDecodeError:raise Unsupported('metadata-not-utf8') from None

    def public_key(self):return str(Pubkey.from_bytes(self.take(32)))

    def finish(self):
        if self.remaining:raise Unsupported('unknown-instruction-tail')
