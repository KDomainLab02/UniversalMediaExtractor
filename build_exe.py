# -*- coding: utf-8 -*-
"""
Build a single-file executable with PyInstaller.

Usage:
    python build_exe.py            # one file, easiest for end users
    python build_exe.py --onedir   # folder build, starts faster

Output: dist/UniversalMediaExtractor(.exe)

Important: PyInstaller cannot cross-compile. A Windows .exe must be built on
Windows, a macOS .app on macOS, a Linux binary on Linux. Use the GitHub
Actions workflow in .github/workflows/build.yml to get all three.
"""
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
NAME = "UniversalMediaExtractor"
SEP = ";" if os.name == "nt" else ":"


def ensure_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
        return True
    except ImportError:
        print("Installing PyInstaller...", flush=True)
        mirror = "https://pypi.tuna.tsinghua.edu.cn/simple"
        if subprocess.call([sys.executable, "-m", "pip", "install", "pyinstaller"]) == 0:
            return True
        return subprocess.call([sys.executable, "-m", "pip", "install",
                                "pyinstaller", "-i", mirror]) == 0


def main():
    if not ensure_pyinstaller():
        print("PyInstaller installation failed. Check your network.")
        return 1

    onedir = "--onedir" in sys.argv
    # macOS: --windowed turns the output into a real .app bundle, so the user
    # can double-click it in Finder instead of starting it from a terminal.
    windowed = "--app" in sys.argv and sys.platform == "darwin"
    icon = os.path.join(BASE, "assets", "icon.ico" if os.name == "nt" else "icon.png")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onedir" if onedir else "--onefile",
        "--windowed" if windowed else "--console",
        "--name", NAME,
    ]
    if os.path.exists(icon):
        cmd += ["--icon", icon]
    cmd += [
        # Web UI
        "--add-data", os.path.join(BASE, "templates") + SEP + "templates",
        # Third-party license texts (GPL v3 for ffmpeg/mutagen, Apache-2.0 for hls.js)
        "--add-data", os.path.join(BASE, "licenses") + SEP + "licenses",
        "--add-data", os.path.join(BASE, "THIRD-PARTY-NOTICES.md") + SEP + ".",
        # Self-invoked helper processes (folder picker / cookie export)
        "--hidden-import", "pick_dir_helper",
        "--hidden-import", "export_cookies",
        # yt-dlp loads extractors dynamically, so all of them must be bundled
        "--collect-all", "yt_dlp",
        # Ships the ffmpeg binary
        "--collect-all", "imageio_ffmpeg",
        # Not needed at runtime
        "--exclude-module", "matplotlib",
        "--exclude-module", "numpy",
        "--exclude-module", "pandas",
        "--exclude-module", "PIL",
        os.path.join(BASE, "app.py"),
    ]

    print("Running PyInstaller, this takes a few minutes...\n", flush=True)
    if subprocess.call(cmd) != 0:
        print("\nBuild failed. See the log above.")
        return 1

    out = os.path.join(BASE, "dist", NAME + (".exe" if os.name == "nt" else ""))
    if not onedir and os.path.exists(out):
        print("\nDone: %s  (%.1f MB)" % (out, os.path.getsize(out) / 1048576))
    else:
        print("\nDone: %s" % os.path.join(BASE, "dist", NAME))
    print("Double-click it; the browser opens automatically.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
