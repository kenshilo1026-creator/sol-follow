"""Bounded latency samples; snapshot reports observed quantiles, not promises."""
from collections import defaultdict,deque
import math


class Metrics:
    def __init__(self):
        self.samples=defaultdict(lambda:deque(maxlen=1000))

    def add(self,name,seconds):
        self.samples[name].append(max(0,seconds)*1000)

    def snapshot(self):
        result={}
        for name,values in self.samples.items():
            ordered=sorted(values)
            if ordered:
                result[name]={'n':len(ordered),**{f'p{p}_ms':round(ordered[max(0,math.ceil(len(ordered)*p/100)-1)],2)
                                               for p in (50,95,99)}}
        return result
