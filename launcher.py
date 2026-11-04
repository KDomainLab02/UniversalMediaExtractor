# -*- coding: utf-8 -*-
"""
Launcher: check the Python version and dependencies, then start app.py.

Why a separate launcher:
Chinese characters inside a Windows .bat file can be mis-parsed by cmd
when it switches code pages (a comment line may be executed as a command).
So start.bat stays pure ASCII and every human-readable message lives here,
where UTF-8 is handled correctly on any system language.
"""
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
REC = ["flask", "yt_dlp", "imageio_ffmpeg", "websocket"]
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
BAR = "=" * 62


def has_deps():
    import importlib
    for name in REC:
        try:
            importlib.import_module(name)
        except Exception:
            return False
    return True


def install():
    req = os.path.join(BASE, "requirements.txt")
    print("First run: installing dependencies (about 1-2 minutes)...\n", flush=True)
    try:
        subprocess.call([sys.executable, "-m", "pip", "install", "--upgrade", "pip"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    if subprocess.call([sys.executable, "-m", "pip", "install", "-r", req]) == 0 and has_deps():
        return True
    print("\nDirect install failed. Retrying with a mirror...\n", flush=True)
    try:
        subprocess.call([sys.executable, "-m", "pip", "install", "-r", req, "-i", MIRROR])
    except Exception:
        pass
    return has_deps()


def main():
    if sys.version_info < (3, 9):
        print("Python %s detected, but 3.9+ is required." % sys.version.split()[0])
        print("Please install a newer version: https://www.python.org/downloads/")
        return 1

    if not has_deps() and not install():
        print("\nDependency installation failed (usually a network issue).")
        print("Run it manually:")
        print("    %s -m pip install -r requirements.txt -i %s" % (sys.executable, MIRROR))
        return 1

    print(BAR)
    print("  Universal Media Extractor")
    print("  Starting... your browser will open http://127.0.0.1:8765")
    print("  Keep this window open while using the tool.")
    print("  Closing this window stops the local server.")
    print(BAR)
    print(flush=True)

    os.chdir(BASE)
    try:
        return subprocess.call([sys.executable, os.path.join(BASE, "app.py")] + sys.argv[1:])
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
