"""linux-gpib backend through ctypes (works with any Python 3, no compiled binding needed)."""

import ctypes
import ctypes.util

from .base import Backend, GpibError, GpibTimeout, timeout_code

# ibsta bits
ERR = 0x8000
TIMO = 0x4000
END = 0x2000

# eos mode flags for ibdev/ibeos
REOS = 0x0400   # terminate reads on the EOS byte
XEOS = 0x0800   # assert EOI with the EOS byte on writes
BIN = 0x1000    # compare all 8 bits of EOS

_IBERR = {
    0: "EDVR", 1: "ECIC", 2: "ENOL", 3: "EADR", 4: "EARG", 5: "ESAC", 6: "EABO",
    7: "ENEB", 8: "EDMA", 10: "EOIP", 11: "ECAP", 12: "EFSO", 14: "EBUS",
    15: "ESTB", 16: "ESRQ", 20: "ETAB",
}

_LIB_CANDIDATES = ["/usr/local/lib/libgpib.so.0", "/usr/lib/libgpib.so.0"]


def _load_library(path=None):
    names = [path] if path else []
    found = ctypes.util.find_library("gpib")
    if found:
        names.append(found)
    names += _LIB_CANDIDATES
    last = None
    for name in names:
        try:
            return ctypes.CDLL(name)
        except OSError as exc:
            last = exc
    raise OSError("libgpib not found (tried %s): %s" % (", ".join(names), last))


class LinuxGpibBackend(Backend):
    name = "linuxgpib"

    def __init__(self, board=0, library=None, eos=0x0A, reos=True, send_eoi=True):
        super().__init__(board)
        lib = self._lib = _load_library(library)
        for fn in ("ibdev", "ibwrt", "ibrd", "ibclr", "ibloc", "ibtrg", "ibrsp", "ibln",
                   "ibsic", "ibonl", "ibtmo", "ThreadIbsta", "ThreadIberr", "ThreadIbcnt"):
            getattr(lib, fn).restype = ctypes.c_int
        lib.ibwrt.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_long]
        lib.ibrd.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_long]
        lib.ibrsp.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_char)]
        lib.ibln.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_short)]
        lib.ThreadIbcntl.restype = ctypes.c_long
        self._eosmode = (eos & 0xFF) | (REOS if reos else 0)
        self._send_eoi = 1 if send_eoi else 0
        self._devs = {}  # (pad, sad) -> device descriptor
        self._chunk = ctypes.create_string_buffer(64 * 1024)

    # -- helpers --------------------------------------------------------------
    def _check(self, status, what):
        if status & ERR:
            err = self._lib.ThreadIberr()
            mnemonic = _IBERR.get(err, str(err))
            cls = GpibTimeout if status & TIMO else GpibError
            raise cls("%s failed: %s (ibsta 0x%04x)" % (what, mnemonic, status & 0xFFFF),
                      code=mnemonic, status=status)
        return status

    def _dev(self, pad, sad, timeout):
        key = (pad, sad)
        ud = self._devs.get(key)
        if ud is None:
            ud = self._lib.ibdev(self.board, pad, sad, timeout_code(timeout), self._send_eoi, self._eosmode)
            if ud < 0:
                self._check(self._lib.ThreadIbsta() | ERR, "ibdev(pad=%d, sad=%d)" % key)
            self._devs[key] = ud
        else:
            self._check(self._lib.ibtmo(ud, timeout_code(timeout)), "ibtmo")
        return ud

    # -- device I/O -------------------------------------------------------
    def write(self, pad, sad, data, timeout=3.0):
        ud = self._dev(pad, sad, timeout)
        self._check(self._lib.ibwrt(ud, data, len(data)), "write to %d" % pad)

    def read(self, pad, sad, max_bytes=1 << 20, timeout=3.0):
        ud = self._dev(pad, sad, timeout)
        out = bytearray()
        size = len(self._chunk)
        while len(out) < max_bytes:
            want = min(size, max_bytes - len(out))
            status = self._lib.ibrd(ud, self._chunk, want)
            count = self._lib.ThreadIbcntl()
            out += self._chunk.raw[:count]
            self._check(status, "read from %d" % pad)
            if status & END:
                break
        return bytes(out)

    def clear(self, pad, sad):
        self._check(self._lib.ibclr(self._dev(pad, sad, 3.0)), "clear %d" % pad)

    def local(self, pad, sad):
        self._check(self._lib.ibloc(self._dev(pad, sad, 3.0)), "local %d" % pad)

    def trigger(self, pad, sad):
        self._check(self._lib.ibtrg(self._dev(pad, sad, 3.0)), "trigger %d" % pad)

    def serial_poll(self, pad, sad):
        byte = ctypes.c_char()
        self._check(self._lib.ibrsp(self._dev(pad, sad, 3.0), ctypes.byref(byte)), "serial poll %d" % pad)
        return byte.value[0] if byte.value else 0

    # -- bus ----------------------------------------------------------------
    def listeners(self, pads=range(1, 31)):
        found = []
        flag = ctypes.c_short()
        for pad in pads:
            self._check(self._lib.ibln(self.board, pad, 0, ctypes.byref(flag)), "ibln %d" % pad)
            if flag.value:
                found.append(pad)
        return found

    def interface_clear(self):
        self._check(self._lib.ibsic(self.board), "ibsic")

    def close(self):
        for ud in self._devs.values():
            self._lib.ibonl(ud, 0)
        self._devs.clear()
