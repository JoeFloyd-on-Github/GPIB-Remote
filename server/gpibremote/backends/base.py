"""Backend interface shared by the linux-gpib and simulated buses.

All methods are blocking and must be called from the board's bus worker thread only.
Addresses are (pad, sad): primary address 0-30, secondary address 0 for none or 0x60-0x7e.
"""

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

    def serial_poll(self, pad, sad):
        """Serial poll; returns the status byte (int)."""
        raise NotImplementedError

    def configure(self, pad, sad, eos=b"\n"):
        """Set read termination for one address: an EOS byte, or None to end on EOI only."""

    def transaction(self, pad, sad, steps, timeout=3.0, delay=0.0, max_bytes=1 << 20):
        """Run several operations on one device with nothing else in between.

        steps: list of ("w", bytes) write, ("q", bytes) write then read, ("r", None) read,
        ("t", None) group execute trigger. Returns the list of bytes read, in order.
        """
        out = []
        for i, (kind, data) in enumerate(steps):
            if i and delay:
                time.sleep(delay)
            if kind in ("w", "q"):
                self.write(pad, sad, data, timeout)
            if kind == "t":
                self.trigger(pad, sad)
            if kind in ("q", "r"):
                out.append(self.read(pad, sad, max_bytes, timeout))
        return out

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
