"""連同一台受管系統代理的本機使用者通道（清單 #28）。主控台（xconsole_server）與桌面版登記（install/setup_local.py）共用。

Linux、macOS 是 Unix socket，Windows 是具名管道。連之前先確認通道是系統代理開的：Unix 看 socket 的擁有者是代理的
專用帳號，Windows 看管道的擁有者是 SYSTEM 或系統管理員；同一台的其他帳號搶先開一個同名的假通道，就連不上。
"""
import functools
import http.client
import io
import os
import socket
import sys
import time

OS = "windows" if os.name == "nt" else ("darwin" if sys.platform == "darwin" else "linux")
# 安裝腳本（install.sh、install.ps1）開的位置與代理的專用帳號
DEFAULT = {"linux": "/run/xconsole/user/agent.sock",
           "darwin": "/Library/Application Support/xConsole/run/agent.sock",
           "windows": "xconsole-user"}[OS]
OWNERS = {"linux": ["xconsole"], "darwin": ["_xconsole"], "windows": ["S-1-5-18", "S-1-5-32-544"]}[OS]


class NotTrusted(OSError):
    pass


@functools.cache
def _win32():
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.ReadFile.argtypes = k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                                      ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    adv.GetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD] + [ctypes.c_void_p] * 5
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    return ctypes, wintypes, k32, adv


class _PipeIO(io.RawIOBase):
    def __init__(self, h, on_close=None):
        self.h, self.on_close = h, on_close
        self.ct, self.wt, self.k32, _ = _win32()

    def close(self):
        if not self.closed and self.on_close:
            self.on_close()
        super().close()

    def readable(self):
        return True

    def writable(self):
        return True

    def readinto(self, b):
        n = self.wt.DWORD()
        buf = (self.ct.c_char * len(b)).from_buffer(b)
        if not self.k32.ReadFile(self.h, buf, len(b), self.ct.byref(n), None):
            if self.ct.get_last_error() in (109, 232):
                return 0
            raise self.ct.WinError(self.ct.get_last_error())
        return n.value

    def write(self, b):
        data, n = bytes(b), self.wt.DWORD()
        if not self.k32.WriteFile(self.h, data, len(data), self.ct.byref(n), None):
            raise BrokenPipeError(self.ct.get_last_error(), "本機通道的另一端關了")
        return n.value


class _PipeSock:
    """給 http.client 用的最小 socket 介面：sendall、makefile、close。
    跟真的 socket 一樣，makefile 出去的讀取端還開著時，close 只做記號，讀取端關了才真的關管道。"""

    def __init__(self, h):
        self.h, self.readers, self.closing = h, 0, False
        self.io = _PipeIO(h)

    def sendall(self, data):
        mv = memoryview(data)
        while mv:
            mv = mv[self.io.write(mv):]

    def makefile(self, mode):
        self.readers += 1
        return io.BufferedReader(_PipeIO(self.h, self._reader_closed))

    def settimeout(self, t):
        pass

    def _reader_closed(self):
        self.readers -= 1
        if self.closing:
            self.close()

    def close(self):
        self.closing = True
        if self.h and not self.readers:
            _win32()[2].CloseHandle(self.h)
            self.h = None


def _pipe_owner(h):
    ct, wt, k32, adv = _win32()
    owner, sd, s = ct.c_void_p(), ct.c_void_p(), wt.LPWSTR()
    # SE_KERNEL_OBJECT、OWNER_SECURITY_INFORMATION
    if adv.GetSecurityInfo(h, 6, 1, ct.byref(owner), None, None, None, ct.byref(sd)) != 0:
        return None
    try:
        return s.value if adv.ConvertSidToStringSidW(owner, ct.byref(s)) else None
    finally:
        k32.LocalFree(s)
        k32.LocalFree(sd)


def _open_pipe(name, owners, timeout):
    ct, _, k32, _ = _win32()
    end = time.time() + timeout
    while True:
        # GENERIC_READ|GENERIC_WRITE、OPEN_EXISTING；SECURITY_SQOS_PRESENT|SECURITY_IDENTIFICATION：對方只能認出我是誰，不能冒用我的身分
        h = k32.CreateFileW(r"\\.\pipe" + "\\" + name, 0xC0000000, 0, None, 3, 0x00110000, None)
        if h not in (None, ct.c_void_p(-1).value):
            break
        err = ct.get_last_error()
        if err != 231 or time.time() > end:       # 231：管道每個執行個體都在忙，稍等再試
            raise ConnectionRefusedError(err, f"連不上本機通道 {name}")
        time.sleep(0.05)
    who = _pipe_owner(h)
    if who not in owners:
        k32.CloseHandle(h)
        raise NotTrusted(f"本機通道 {name} 不是系統代理開的（擁有者 {who}），不連")
    return _PipeSock(h)


def _open_unix(path, owners, timeout):
    import pwd
    want = set()
    for o in owners:
        try:
            want.add(pwd.getpwnam(o).pw_uid)
        except KeyError:
            continue
    st, dst = os.stat(path), os.stat(os.path.dirname(path))
    if st.st_uid not in want or dst.st_uid not in want | {0} or dst.st_mode & 0o022:
        raise NotTrusted(f"本機通道 {path} 不是系統代理開的（擁有者 uid {st.st_uid}），不連")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
    except OSError:
        s.close()
        raise
    return s


class Conn(http.client.HTTPConnection):
    def __init__(self, where=None, owners=None, timeout=40):
        super().__init__("localhost", timeout=timeout)
        self.where, self.owners = where or DEFAULT, owners or OWNERS

    def connect(self):
        self.sock = (_open_pipe if OS == "windows" else _open_unix)(self.where, self.owners, self.timeout or 40)


def request(method, path, body=None, ctype="application/json", where=None, owners=None, timeout=40):
    """送一個請求，回 http.client 的回應（讀完或關掉時連線一起關）。"""
    c = Conn(where, owners, timeout)
    c.request(method, path, body=body, headers={"Content-Type": ctype, "Connection": "close"})
    return c.getresponse()


def managed(where=None, owners=None):
    """這台有沒有受管的系統代理在聽本機通道。"""
    try:
        r = request("GET", "/healthz", where=where, owners=owners, timeout=5)
        with r:
            return r.status == 200 and b"xconsole-agent" in r.read(4096)
    except (OSError, http.client.HTTPException):
        return False
