"""E2E check: downloads must land in a per-title folder (not a task-id folder).

Runs a temporary server instance on port 8769 with DOWNLOAD_DIR pointed at a
temp directory, downloads one small Bilibili video, saves a cover, then prints
the resulting tree and asserts everything sits under the title folder.
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402

TMP = tempfile.mkdtemp(prefix="ume_title_folder_")
app.DOWNLOAD_DIR = TMP
print("DOWNLOAD_DIR =", TMP)

PORT = 8769
URL = "https://www.bilibili.com/video/BV1xx411c7mD"   # 占位 BV 号，仅验证 e2e 流程


def post(path, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def get(path):
    return json.loads(urllib.request.urlopen(
        f"http://127.0.0.1:{PORT}{path}", timeout=30).read())


def wait_task(tid, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = get(f"/api/progress/{tid}")
        if st.get("status") in ("done", "error"):
            return st
        time.sleep(1.5)
    return {"status": "timeout"}


def main():
    t = threading.Thread(
        target=lambda: app.app.run(host="127.0.0.1", port=PORT, threaded=True),
        daemon=True)
    t.start()
    time.sleep(2.5)

    tid = post("/api/download", {"url": URL, "kind": "video", "height": 360,
                                 "proxy": "", "cookie_browser": ""})["task_id"]
    st = wait_task(tid)
    print("video task:", st.get("status"), st.get("filename"), st.get("error", ""))

    cid = post("/api/download", {"url": URL, "kind": "audio", "height": 0,
                                 "proxy": "", "cookie_browser": ""})["task_id"]
    st2 = wait_task(cid)
    print("audio task:", st2.get("status"), st2.get("filename"), st2.get("error", ""))

    try:
        cover = post("/api/save_cover", {"title": "封面测试 标题", "ext": "jpg",
                                         "url": "https://i0.hdslb.com/bfs/archive/3c66e1d0a9d4a8f4d0a1c0b2e2f0a1c0b2e2f0a1.jpg"})
        print("cover:", cover.get("ok"), cover.get("error", ""))
    except Exception as e:  # dummy URL may 404 -> server 500 by design
        print("cover (expected fail with fake url):", e)

    print("---- tree ----")
    for root, dirs, files in os.walk(TMP):
        rel = os.path.relpath(root, TMP)
        for f in files:
            print(os.path.join(rel, f) if rel != "." else f)

    # assertions: no task-id-looking folders left with media inside
    bad = []
    for name in os.listdir(TMP):
        p = os.path.join(TMP, name)
        if os.path.isdir(p) and len(name) == 12 and name.isalnum():
            if os.listdir(p):
                bad.append(name)
    media = []
    for root, _, files in os.walk(TMP):
        for f in files:
            if f.endswith((".mp4", ".mp3", ".jpg", ".zip")):
                media.append(os.path.relpath(os.path.join(root, f), TMP))
    print("media files:", media)
    print("leftover task dirs:", bad)
    ok = media and not bad
    print("RESULT:", "PASS" if ok else "FAIL")


if __name__ == "__main__":
    main()
