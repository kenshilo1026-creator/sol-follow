"""Local diagnostics immediately; one alert per sustained outage episode."""
import time


class Availability:
    def __init__(self, notices, key):
        self.notices, self.key = notices, key
        self.since = None
        self.notified = False

    def failure(self, kind, detail):
        now = time.monotonic()
        if self.since is None:
            self.since = now
        alert = now-self.since > 60 and not self.notified
        if alert:
            self.notified = True
        self.notices.emit(kind, detail, alert=alert,
                          key=self.key+(':alert' if alert else ':local'), interval=60)

    def recovered(self, kind=None, detail=None):
        if self.notified and kind:
            self.notices.emit(kind, detail or {}, alert=True, key=self.key+':recovered')
        self.since, self.notified = None, False
