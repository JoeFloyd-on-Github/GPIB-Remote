"""Backend interface shared by the linux-gpib and simulated buses.

All methods are blocking and must be called from the board's bus worker thread only.
Addresses are (pad, sad): primary address 0-30, secondary address 0 for none or 0x60-0x7e.
"""

import re
import time

# NI-488 timeout codes (T10us .. T1000s) and their durations in seconds.
_TIMEOUTS = [
    (0, 0.0), (1, 10e-6), (2, 30e-6), (3, 100e-6), (4, 300e-6), (5, 1e-3), (6, 3e-3),
    (7, 10e-3), (8, 30e-3), (9, 100e-3), (10, 300e-3), (11, 1.0), (12, 3.0), (13, 10.0),
    (14, 30.0), (15, 100.0), (16, 300.0), (17, 1000.0),
]


def timeout_code(seconds):
    """Smallest NI-488 timeout code that is at least `seconds` (0 = wait forever)."""
    if seconds is None or seconds <= 0:
        return 0
    for code, dur in _TIMEOUTS[1:]:
        if dur >= seconds:
            return code
    return 17


class GpibError(Exception):
    """A bus operation failed. `code` is the NI-488 iberr mnemonic when known."""

    def __init__(self, msg, code=None, status=0):
        super().__init__(msg)
        self.code = code
        self.status = status


class GpibTimeout(GpibError):
    pass


class Backend:
    """One GPIB board (interface)."""

    name = "abstract"

    def __init__(self, board=0):
        self.board = board

    # -- device I/O -------------------------------------------------------
    def write(self, pad, sad, data, timeout=3.0):
        """Send bytes to a device (EOI asserted on the last byte)."""
        raise NotImplementedError

    def read(self, pad, sad, max_bytes=1 << 20, timeout=3.0):
        """Read until END (EOI or EOS) or max_bytes. Returns bytes."""
        raise NotImplementedError

    def query(self, pad, sad, data, max_bytes=1 << 20, timeout=3.0):
        self.write(pad, sad, data, timeout)
        return self.read(pad, sad, max_bytes, timeout)

    def clear(self, pad, sad):
        """Selected device clear (SDC)."""
        raise NotImplementedError

    def local(self, pad, sad):
        """Return the device to front-panel (local) control (GTL)."""
        raise NotImplementedError

    def trigger(self, pad, sad):
        """Group execute trigger (GET) to one device."""
        raise NotImplementedError

    def serial_poll(self, pad, sad, timeout=3.0):
        """Serial poll; returns the status byte (int)."""
        raise NotImplementedError

    def configure(self, pad, sad, eos=b"\n"):
        """Set read termination for one address: an EOS byte, or None to end on EOI only."""

    def transaction(self, pad, sad, steps, timeout=3.0, delay=0.0, max_bytes=1 << 20):
        """Run several operations on one device with nothing else in between.

        steps: list of ("w", bytes) write, ("q", bytes) write then read, ("r", None) read,
        ("t", None) group execute trigger, ("p", None) serial poll (result discarded),
        ("e", (query, none_regex, limit)) empty the error queue: ask `query` until the answer
        matches `none_regex` (bytes), at most `limit` times; gives the errors found, one per line.
        ("b", bytes) write a query, then read a binary definite-length block (#<n><length><data>)
        and give its data bytes.
        Returns the list of bytes read, in order.
        """
        out = []
        for i, (kind, data) in enumerate(steps):
            if i and delay:
                time.sleep(delay)
            if kind in ("w", "q"):
                self.write(pad, sad, data, timeout)
            if kind == "t":
                self.trigger(pad, sad)
            if kind == "p":
                self.serial_poll(pad, sad, timeout)
            if kind == "e":
                query, none_re, limit = data
                found = []
                for _ in range(limit):
                    self.write(pad, sad, query, timeout)
                    answer = self.read(pad, sad, max_bytes, timeout).strip()
                    if re.search(none_re, answer):
                        break
                    found.append(answer)
                out.append(b"\n".join(found))
            if kind in ("q", "r"):
                out.append(self.read(pad, sad, max_bytes, timeout))
            if kind == "b":
                self.write(pad, sad, data, timeout)
                out.append(self.read_block(pad, sad, max_bytes, timeout))
        return out

    def read_block(self, pad, sad, max_bytes=1 << 20, timeout=3.0):
        """Read an IEEE 488.2 definite-length block and the terminator after it; return the data.

        Reads that end on the EOS byte stop early wherever the data holds that byte, so this
        keeps reading until the length the header gives has arrived.
        """
        buf = self.read(pad, sad, max_bytes, timeout)
        start = buf.find(b"#")
        while start < 0 and len(buf) < 64:
            buf += self.read(pad, sad, max_bytes, timeout)
            start = buf.find(b"#")
        if start < 0:
            raise GpibError("expected a #-block from %d, got %r" % (pad, buf[:40]))
        while len(buf) < start + 2:
            buf += self.read(pad, sad, max_bytes, timeout)
        if not buf[start + 1:start + 2].isdigit() or buf[start + 1:start + 2] == b"0":
            raise GpibError("only definite-length blocks are supported (got %r)" % buf[start:start + 12])
        digits = int(buf[start + 1:start + 2])
        while len(buf) < start + 2 + digits:
            buf += self.read(pad, sad, max_bytes, timeout)
        length = int(buf[start + 2:start + 2 + digits])
        begin = start + 2 + digits
        end = begin + length
        if end > max_bytes:
            raise GpibError("block of %d bytes is larger than the %d allowed" % (length, max_bytes))
        while len(buf) < end:
            buf += self.read(pad, sad, max_bytes, timeout)
        if len(buf) == end:     # the last read stopped on a data byte equal to EOS: the terminator is still to come
            try:
                self.read(pad, sad, 16, timeout)
            except GpibTimeout:
                pass
        return buf[begin:end]

    # -- bus ----------------------------------------------------------------
    def listeners(self, pads=range(1, 31)):
        """Primary addresses with a device present. Sends no device-dependent commands."""
        raise NotImplementedError

    def interface_clear(self):
        """Pulse IFC: all devices unaddressed, controller takes control."""
        raise NotImplementedError

    def close(self):
        pass

    def info(self):
        return {"backend": self.name, "board": self.board}
