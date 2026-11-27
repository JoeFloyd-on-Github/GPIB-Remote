"""Where to find the server: command line, then environment, then ~/.gpibctl.ini, then localhost:9000.

~/.gpibctl.ini (on Windows %USERPROFILE%\\.gpibctl.ini):
    [server]
    host = 192.168.1.157
    port = 9000
"""

import configparser
import os

PATH = os.path.join(os.path.expanduser("~"), ".gpibctl.ini")


def defaults():
    cp = configparser.ConfigParser()
    cp.read(PATH)
    host = os.environ.get("GPIBREMOTE_HOST") or cp.get("server", "host", fallback="localhost")
    port = os.environ.get("GPIBREMOTE_PORT") or cp.get("server", "port", fallback="9000")
    return host, int(port)


def save(host, port):
    cp = configparser.ConfigParser()
    cp.read(PATH)
    if not cp.has_section("server"):
        cp.add_section("server")
    cp.set("server", "host", host)
    cp.set("server", "port", str(port))
    with open(PATH, "w") as fh:
        cp.write(fh)
    return PATH
