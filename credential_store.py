"""Store dashboard connections with Windows user-scoped data protection."""

import ctypes
import json
import os
import tempfile
from ctypes import wintypes


class DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect(data, decrypt=False):
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel.LocalFree.restype = ctypes.c_void_p
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = (ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob))
    operation.restype = wintypes.BOOL
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = DataBlob(len(data), buffer)
    result = DataBlob()
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


class CredentialStore:
    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        state = json.loads(protect(data, decrypt=True))
        if not isinstance(state, dict):
            raise ValueError("Invalid saved account state.")
        return state

    def save(self, state):
        encrypted = protect(json.dumps(state).encode("utf-8"))
        file = tempfile.NamedTemporaryFile(dir=self.path.parent, delete=False)
        try:
            with file:
                file.write(encrypted)
                file.flush()
                os.fsync(file.fileno())
            os.replace(file.name, self.path)
        finally:
            if os.path.exists(file.name):
                os.unlink(file.name)
