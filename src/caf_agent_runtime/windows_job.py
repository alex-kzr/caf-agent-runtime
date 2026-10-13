"""Per-execution Windows Job Object ownership for a gated subprocess tree."""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes


class ProcessTreeCleanupError(RuntimeError):
    """The task's process tree could not be verified stopped."""


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _BasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


if os.name == "nt":
    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel.CreateJobObjectW.restype = ctypes.c_void_p
    _kernel.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel.SetInformationJobObject.restype = wintypes.BOOL
    _kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel.OpenProcess.restype = ctypes.c_void_p
    _kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel.QueryInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                                   wintypes.DWORD, ctypes.c_void_p]
    _kernel.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel.TerminateJobObject.argtypes = [ctypes.c_void_p, wintypes.UINT]
    _kernel.TerminateJobObject.restype = wintypes.BOOL
    _kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    _kernel.CloseHandle.restype = wintypes.BOOL


class WindowsJob:
    """Own only the gated supervisor and descendants of one execution."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows Job Objects require Windows")
        self._handle = _kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise ProcessTreeCleanupError(f"CreateJobObject failed: {ctypes.get_last_error()}")
        self._closed = False
        self._assigned = False
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel.SetInformationJobObject(self._handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.get_last_error()
            self.close()
            raise ProcessTreeCleanupError(f"SetInformationJobObject failed: {error}")

    def assign(self, pid: int) -> None:
        if self._closed or self._assigned:
            raise ProcessTreeCleanupError("job is closed or already assigned")
        # The supervisor waits for a one-byte GO signal, so it cannot spawn
        # its target or children before assignment completes.
        process = _kernel.OpenProcess(0x0100 | 0x0001 | 0x1000, False, pid)
        if not process:
            raise ProcessTreeCleanupError(f"OpenProcess failed: {ctypes.get_last_error()}")
        try:
            if not _kernel.AssignProcessToJobObject(self._handle, process):
                raise ProcessTreeCleanupError(
                    f"AssignProcessToJobObject failed: {ctypes.get_last_error()}"
                )
            self._assigned = True
        finally:
            _kernel.CloseHandle(process)

    def active_processes(self) -> int:
        if self._closed:
            raise ProcessTreeCleanupError("job is closed")
        info = _BasicAccountingInformation()
        if not _kernel.QueryInformationJobObject(self._handle, 1, ctypes.byref(info),
                                                 ctypes.sizeof(info), None):
            raise ProcessTreeCleanupError(
                f"QueryInformationJobObject failed: {ctypes.get_last_error()}"
            )
        return int(info.ActiveProcesses)

    def terminate_and_verify(self, timeout_seconds: float = 5) -> None:
        if self._closed:
            raise ProcessTreeCleanupError("job was closed before cleanup verification")
        if self._assigned and not _kernel.TerminateJobObject(self._handle, 1):
            raise ProcessTreeCleanupError(f"TerminateJobObject failed: {ctypes.get_last_error()}")
        deadline = time.monotonic() + timeout_seconds
        while self._assigned and self.active_processes():
            if time.monotonic() >= deadline:
                raise ProcessTreeCleanupError("job still has live processes after termination")
            time.sleep(.02)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if not _kernel.CloseHandle(self._handle):
                raise ProcessTreeCleanupError(f"CloseHandle failed: {ctypes.get_last_error()}")
