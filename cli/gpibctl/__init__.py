"""gpibctl: command-line client for the GPIB-Remote server (plain TCP, JSON Lines)."""

import getpass
import json
import socket


class Client:
    def __init__(self, host="localhost", port=9000, timeout=30.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.file = self.sock.makefile("rwb")
        self.next_id = 1

    def request(self, op, **params):
        rid = self.next_id
        self.next_id += 1
        msg = dict(params, id=rid, op=op)
        self.file.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.file.flush()
        while True:
            line = self.file.readline()
            if not line:
                raise ConnectionError("server closed the connection")
            resp = json.loads(line)
            if resp.get("id") == rid:
                break  # events (no id) are ignored in this simple client
        if not resp.get("ok"):
            err = resp.get("error", {})
            raise RuntimeError("%s: %s" % (err.get("code"), err.get("msg")))
        return resp.get("result")

    def hello(self, client="gpibctl"):
        return self.request("hello", client=client, user=getpass.getuser(), host=socket.gethostname())

    def close(self):
        self.file.close()
        self.sock.close()
