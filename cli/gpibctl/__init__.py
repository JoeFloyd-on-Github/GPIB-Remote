"""gpibctl: command-line client for the GPIB-Remote server (plain TCP, JSON Lines)."""

import collections
import getpass
import json
import socket
import time


class ServerError(RuntimeError):
    """The server answered ok=false. `code` and `error` hold the details."""

    def __init__(self, error):
        self.error = error or {}
        self.code = self.error.get("code")
        msg = "%s: %s" % (self.code, self.error.get("msg"))
        for e in self.error.get("errors") or []:
            msg += "\n  " + e
        super().__init__(msg)


class Client:
    def __init__(self, host="localhost", port=9000, timeout=30.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.file = self.sock.makefile("rwb")
        self.next_id = 1
        self.events = collections.deque()

    def _readline(self):
        line = self.file.readline()
        if not line:
            raise ConnectionError("server closed the connection")
        return json.loads(line)

    def request(self, op, **params):
        rid = self.next_id
        self.next_id += 1
        msg = dict(params, id=rid, op=op)
        self.file.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.file.flush()
        while True:
            resp = self._readline()
            if "event" in resp and "id" not in resp:
                self.events.append(resp)
                continue
            if resp.get("id") == rid:
                break
        if not resp.get("ok"):
            raise ServerError(resp.get("error"))
        return resp.get("result")

    def wait_event(self, match=None, timeout=10.0):
        """Return the next event for which match(event) is true (any event if match is None)."""
        deadline = time.monotonic() + timeout
        while True:
            while self.events:
                ev = self.events.popleft()
                if match is None or match(ev):
                    return ev
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("no matching event")
            self.sock.settimeout(left)
            try:
                ev = self._readline()
            except socket.timeout:
                raise TimeoutError("no matching event")
            finally:
                self.sock.settimeout(None)
            self.events.append(ev)

    def hello(self, client="gpibctl", user=None, host=None):
        return self.request("hello", client=client, user=user or getpass.getuser(),
                            host=host or socket.gethostname())

    def close(self):
        self.file.close()
        self.sock.close()
