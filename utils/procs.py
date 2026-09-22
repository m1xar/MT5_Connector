from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes

TH32CS_SNAPPROCESS = 0x2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x1
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
_EPOCH_AS_FILETIME = 116_444_736_000_000_000


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _kernel32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


def _image_path(k32, pid: int) -> str | None:
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if k32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return None
    finally:
        k32.CloseHandle(handle)


def _processes_named(name: str) -> list[tuple[int, str]]:
    if sys.platform != "win32":
        return []
    k32 = _kernel32()
    snapshot = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return []
    entry = _ProcessEntry()
    entry.dwSize = ctypes.sizeof(entry)
    found: list[tuple[int, str]] = []
    try:
        more = k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.szExeFile.lower() == name:
                path = _image_path(k32, entry.th32ProcessID)
                if path:
                    found.append((entry.th32ProcessID, os.path.normcase(path)))
            more = k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)
    return found


def pids_of(exe_path: str) -> list[int]:
    wanted = os.path.normcase(os.path.abspath(exe_path))
    return [pid for pid, path in _processes_named(os.path.basename(exe_path).lower()) if path == wanted]


def updater_pids(exe_name: str = "terminal64.exe") -> list[tuple[int, str]]:
    return [(pid, path) for pid, path in _processes_named(exe_name) if os.sep + "liveupdate" + os.sep in path]


def started_at(pid: int) -> float | None:
    if sys.platform != "win32":
        return None
    k32 = _kernel32()
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        creation = times[0]
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return (ticks - _EPOCH_AS_FILETIME) / 10_000_000
    finally:
        k32.CloseHandle(handle)


def terminate(pid: int) -> bool:
    k32 = _kernel32()
    handle = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(k32.TerminateProcess(handle, 1))
    finally:
        k32.CloseHandle(handle)


def terminate_all(exe_path: str, wait_seconds: float = 10.0) -> list[int]:
    pids = pids_of(exe_path)
    for pid in pids:
        terminate(pid)
    deadline = time.monotonic() + wait_seconds
    while pids and pids_of(exe_path) and time.monotonic() < deadline:
        time.sleep(0.5)
    return pids


def kill_stale_updaters(older_than_seconds: float) -> list[tuple[int, str]]:
    cutoff = time.time() - older_than_seconds
    stale = [(pid, path) for pid, path in updater_pids() if (started_at(pid) or time.time()) < cutoff]
    for pid, _ in stale:
        terminate(pid)
    return stale
