# -*- coding: utf-8 -*-
"""
Native "pick a folder" helper (standalone process).

Why a separate process:
  1. The Flask worker thread may already be initialized as MTA by another
     library; showing an IFileDialog from such a thread fails silently.
  2. A native dialog is modal and blocking; running it inside a request
     thread keeps that request (and its socket) occupied for minutes.
  3. A separate process can use the current foreground window as the dialog
     owner, so the picker always shows up in front of the browser.

Usage:
    python pick_dir_helper.py "<title>" "<initial directory>"

Exit codes:
    0  picked  -> UTF-8 path on stdout
    2  cancelled by the user
    1  error   -> reason on stderr
"""
import ctypes
import os
import sys
from ctypes import POINTER, byref, c_void_p, c_wchar_p, c_ulong, c_ushort, c_ubyte


class GUID(ctypes.Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", c_ushort),
                ("Data3", c_ushort), ("Data4", c_ubyte * 8)]


def _guid(s):
    import uuid
    u = uuid.UUID(s)
    g = GUID()
    g.Data1, g.Data2, g.Data3 = u.time_low, u.time_mid, u.time_hi_version
    g.Data4[:] = u.bytes[8:]
    return g


def _vtbl_fn(obj, index, restype, *argtypes):
    """Return the COM method at vtable slot `index` as a callable."""
    vtbl = ctypes.cast(ctypes.cast(obj, POINTER(c_void_p))[0], POINTER(c_void_p))
    return ctypes.cast(vtbl[index], ctypes.WINFUNCTYPE(restype, *argtypes))


def win_pick(title, initial):
    """Show IFileOpenDialog in folder mode. Returns (status, path)."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32")
    shell32 = ctypes.WinDLL("shell32")

    # Keep the dialog crisp and correctly sized on scaled (125%/150%) displays.
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)   # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

    ole32.CoInitializeEx.argtypes = [c_void_p, c_ulong]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoCreateInstance.argtypes = [POINTER(GUID), c_void_p, c_ulong,
                                       POINTER(GUID), POINTER(c_void_p)]
    ole32.CoCreateInstance.restype = ctypes.c_long

    hr = ole32.CoInitializeEx(None, 2)          # COINIT_APARTMENTTHREADED
    # 0x80010106 = RPC_E_CHANGED_MODE (already initialized with another model)
    if hr not in (0, 1) and (hr & 0xFFFFFFFF) != 0x80010106:
        return "error", "CoInitializeEx failed: 0x%08X" % (hr & 0xFFFFFFFF)

    ppv = c_void_p()
    clsid = _guid("DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7")   # FileOpenDialog
    iid = _guid("D57C7288-D4AD-4768-BE02-9D969532D960")     # IFileOpenDialog
    hr = ole32.CoCreateInstance(byref(clsid), None, 1, byref(iid), byref(ppv))
    if hr != 0 or not ppv:
        return "error", "cannot create dialog: 0x%08X" % (hr & 0xFFFFFFFF)

    try:
        # IFileDialog vtable, in official IDL order:
        #   3 Show / 9 SetOptions / 12 SetFolder / 15 SetFileName / 16 GetFileName
        #  17 SetTitle / 18 SetOkButtonLabel / 19 SetFileNameLabel / 20 GetResult
        #  The OK button label is left to the system so it stays localized.
        # Note: GetResult is slot 20. Calling slot 18 returns S_OK with a NULL
        # result, which silently turns every successful pick into a "cancel".
        Show = _vtbl_fn(ppv, 3, ctypes.c_long, c_void_p, c_void_p)
        SetOptions = _vtbl_fn(ppv, 9, ctypes.c_long, c_void_p, c_ulong)
        SetFolder = _vtbl_fn(ppv, 12, ctypes.c_long, c_void_p, c_void_p)
        SetTitle = _vtbl_fn(ppv, 17, ctypes.c_long, c_void_p, c_wchar_p)
        GetResult = _vtbl_fn(ppv, 20, ctypes.c_long, c_void_p, POINTER(c_void_p))

        if title:
            SetTitle(ppv, title)                 # may return E_FAIL on some builds
        SetOptions(ppv, 0x20 | 0x40)             # FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM

        if initial and os.path.isdir(initial):
            shell32.SHCreateItemFromParsingName.argtypes = [
                c_wchar_p, c_void_p, POINTER(GUID), POINTER(c_void_p)]
            shell32.SHCreateItemFromParsingName.restype = ctypes.c_long
            iid_si = _guid("43826d1e-e718-42ee-bc55-a1e261c37bfe")   # IShellItem
            psi = c_void_p()
            if shell32.SHCreateItemFromParsingName(initial, None, byref(iid_si), byref(psi)) == 0 and psi:
                SetFolder(ppv, psi)
                _vtbl_fn(psi, 2, c_ulong, c_void_p)(psi)         # Release

        # Owned by the foreground window (the browser), so it opens in front.
        owner = user32.GetForegroundWindow()
        hr = Show(ppv, owner if owner else None) & 0xFFFFFFFF
        if hr == 0x800704C7:                     # ERROR_CANCELLED
            return "cancel", ""
        if hr != 0:
            return "error", "dialog failed: 0x%08X" % hr

        pres = c_void_p()
        hr = GetResult(ppv, byref(pres)) & 0xFFFFFFFF
        if hr != 0 or not pres:
            return "error", "no result from dialog: 0x%08X" % hr

        GetDisplayName = _vtbl_fn(pres, 5, ctypes.c_long, c_void_p, c_ulong, POINTER(c_wchar_p))
        pw = c_wchar_p()
        hr = GetDisplayName(pres, 0x80058000, byref(pw)) & 0xFFFFFFFF   # SIGDN_FILESYSPATH
        path = (pw.value or "") if hr == 0 else ""
        _vtbl_fn(pres, 2, c_ulong, c_void_p)(pres)                       # Release
        if not path:
            return "error", "selected item has no filesystem path: 0x%08X" % hr
        return "ok", path
    finally:
        try:
            _vtbl_fn(ppv, 2, c_ulong, c_void_p)(ppv)
        except Exception:
            pass
        try:
            ole32.CoUninitialize()
        except Exception:
            pass


def main():
    title = sys.argv[1] if len(sys.argv) > 1 else "Select Folder"
    initial = sys.argv[2] if len(sys.argv) > 2 else ""

    if os.name == "nt":
        status, path = win_pick(title, initial)
    else:
        status, path = "error", "this helper is Windows only"

    if status == "ok":
        sys.stdout.buffer.write(path.encode("utf-8"))
        sys.stdout.buffer.flush()
        return 0
    if status == "cancel":
        return 2
    sys.stderr.buffer.write(path.encode("utf-8"))
    sys.stderr.buffer.flush()
    return 1


if __name__ == "__main__":
    sys.exit(main())
