"""Fakes for alpaca_feed tests: an HTTP response, a session that replays them, and a bar dict."""


class Resp:
    def __init__(self, status, body=None, headers=None, json_error=False):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}
        self.text, self._json_error = str(body), json_error

    def json(self):
        if self._json_error:
            raise ValueError("Expecting value")
        return self._body


class Session:
    """Replays responses in order; an Exception instance in the list is raised instead."""

    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, headers, params, timeout):
        self.calls.append((url, headers, dict(params)))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def bar(t, c=500.0):
    return {"t": t, "o": c, "h": c + 1, "l": c - 1, "c": c, "v": 1000, "n": 10, "vw": c}
