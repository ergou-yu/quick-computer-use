"""Lazy Win32 process probes; no subprocess startup or UIA/COM imports."""
from functools import lru_cache


@lru_cache(maxsize=1)
def _kernel():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def is_process_alive(pid: int) -> bool:
    """Probe a process handle without sending a signal or waiting.

    Access denied conservatively means alive, so callers never reclaim a
    profile belonging to an inaccessible process. Invalid/dead PIDs are false.
    """
    import ctypes

    kernel = _kernel()
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED
    try:
        return kernel.WaitForSingleObject(handle, 0) != 0  # WAIT_OBJECT_0 = exited
    finally:
        kernel.CloseHandle(handle)


def listening_ports(pid: int) -> list[int]:
    """Query IPv4/IPv6 listener ownership directly, independent of locale."""
    import ctypes
    from ctypes import wintypes
    import socket

    class TCP4Row(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in
                    ("state", "local_addr", "local_port", "remote_addr", "remote_port", "pid")]

    class TCP6Row(ctypes.Structure):
        _fields_ = [("local_addr", ctypes.c_ubyte * 16), ("local_scope", wintypes.DWORD),
                    ("local_port", wintypes.DWORD), ("remote_addr", ctypes.c_ubyte * 16),
                    ("remote_scope", wintypes.DWORD), ("remote_port", wintypes.DWORD),
                    ("state", wintypes.DWORD), ("pid", wintypes.DWORD)]

    table = ctypes.WinDLL("iphlpapi").GetExtendedTcpTable
    table.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                      wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
    table.restype = wintypes.DWORD
    ports = set()
    for family, row_type in ((socket.AF_INET, TCP4Row), (socket.AF_INET6, TCP6Row)):
        size = wintypes.DWORD()
        code = table(None, ctypes.byref(size), False, family, 3, 0)  # OWNER_PID_LISTENER
        for _ in range(3):  # A listener can appear between size and data reads.
            if code not in (0, 122):  # ERROR_INSUFFICIENT_BUFFER
                raise OSError(code, "GetExtendedTcpTable failed")
            allocated = max(size.value, 4)
            buffer = ctypes.create_string_buffer(allocated)
            code = table(buffer, ctypes.byref(size), False, family, 3, 0)
            if code == 122:
                continue
            if code != 0:
                raise OSError(code, "GetExtendedTcpTable failed")
            count = wintypes.DWORD.from_buffer(buffer).value
            row_size = ctypes.sizeof(row_type)
            if 4 + count * row_size > allocated:
                raise OSError("Invalid TCP ownership table size")
            for index in range(count):
                row = row_type.from_buffer(buffer, 4 + index * row_size)
                if row.pid == pid and row.state == 2:  # LISTEN
                    ports.add(socket.ntohs(row.local_port & 0xFFFF))
            break
        else:
            raise OSError("TCP ownership table kept growing")
    return sorted(ports)
