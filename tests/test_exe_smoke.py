# -*- coding: utf-8 -*-
"""Smoke test for the packaged exe: startup time + folder-picker self-invoke."""
import ctypes
import os
import subprocess
import sys
import time
import urllib.request
from ctypes import wintypes

# Path to the packaged exe; override with: python test_exe_smoke.py <path-to-exe>
EXE = os.environ.get("UME_EXE") or (sys.argv[1] if len(sys.argv) > 1
                                    else os.path.join("dist", "UniversalMediaExtractor.exe"))
PORT = "8772"
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

user32 = ctypes.windll.user32
EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)


def dialogs_of(pid):
    """pid=None matches any visible #32770 dialog."""
    out = []

    def cb(h, _):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if pid is None or p.value == pid:
            t = ctypes.create_unicode_buffer(300)
            c = ctypes.create_unicode_buffer(100)
            user32.GetWindowTextW(h, t, 300)
            user32.GetClassNameW(h, c, 100)
            if c.value == "#32770" and user32.IsWindowVisible(h) and t.value:
                out.append((h, t.value))
        return True

    user32.EnumWindows(EnumProc(cb), None)
    return out


def wait_up(url, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return time.time() - t0
        except Exception:
            time.sleep(0.2)
    return None


print("=== 1. startup time (onefile unpack + flask boot) ===")
env = dict(os.environ, PORT=PORT, NO_BROWSER="1")
t0 = time.time()
srv = subprocess.Popen([EXE], env=env, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL,
                       cwd=os.path.dirname(os.path.abspath(EXE)) or None)
dt = wait_up("http://127.0.0.1:" + PORT + "/")
print("   homepage ready in %.1f s" % dt if dt else "   FAILED to start")
if dt:
    body = urllib.request.urlopen("http://127.0.0.1:" + PORT + "/").read()
    print("   first byte contains title:", "万能" in body.decode("utf-8", "replace"))
print("   download dir:", urllib.request.urlopen(
    "http://127.0.0.1:" + PORT + "/api/get_download_dir").read().decode())

print("\n=== 2. folder picker via exe self-invoke (--pick-dir) ===")
# A onefile build runs Python in a child of the bootloader, so the dialog
# belongs to a different PID than the one Popen returned. Match by title.
p = subprocess.Popen([EXE, "--pick-dir", "Select download folder",
                      os.path.expanduser("~")],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     creationflags=subprocess.CREATE_NO_WINDOW)
hwnd = None
for _ in range(160):
    time.sleep(0.25)
    d = dialogs_of(None)
    if d:
        hwnd = d[0][0]
        print("   dialog appeared:", d[0][1])
        break
if not hwnd:
    print("   NO DIALOG -> self-invoke path is broken")
else:
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.5)
    VK_RETURN = 0x0D
    user32.keybd_event(VK_RETURN, 0, 0, 0)
    user32.keybd_event(VK_RETURN, 0, 2, 0)
    try:
        out, err = p.communicate(timeout=30)
        picked = (out or b"").decode("utf-8", "replace").strip()
        print("   exit code", p.returncode, "| picked:", picked)
        print("   RESULT:", "OK" if p.returncode == 0 and os.path.isdir(picked) else "FAIL")
    except subprocess.TimeoutExpired:
        print("   dialog did not close after Enter")
        p.kill()

srv.terminate()
print("\ndone")
