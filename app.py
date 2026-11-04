# -*- coding: utf-8 -*-
"""
万能素材提取工具 / Universal Media Extractor
后端基于 yt-dlp，支持从 YouTube / TikTok / Instagram / Twitter/X / B站 / 抖音 等上千个平台
提取 视频、图集、音乐 等各类素材
"""
import os
import re
import sys
import uuid
import json
import base64
import http.client
import pathlib
import shutil
import sqlite3
import threading
import time
import zipfile
import subprocess
from concurrent.futures import ThreadPoolExecutor

# A newer yt-dlp downloaded at runtime ("core update") lives in <app dir>/core
# and must win over the bundled copy, so it has to be on sys.path *before*
# the import below. This keeps a frozen build usable when platforms change.
_FROZEN_EARLY = bool(getattr(sys, "frozen", False))
_BASE_EARLY = (os.path.dirname(os.path.abspath(sys.executable)) if _FROZEN_EARLY
               else os.path.dirname(os.path.abspath(__file__)))
CORE_DIR = os.path.join(_BASE_EARLY, "core")
if os.path.isdir(os.path.join(CORE_DIR, "yt_dlp")):
    sys.path.insert(0, CORE_DIR)

from flask import Flask, request, jsonify, send_file, render_template, Response
import yt_dlp
from yt_dlp.utils import sanitized_Request
from imageio_ffmpeg import get_ffmpeg_exe

import atexit
import random
import tempfile
import urllib.error
import urllib.parse
import urllib.request

try:
    import websocket  # websocket-client，抖音浏览器通道（CDP）用
except Exception:
    websocket = None

# When running from source, code and user data share one folder.
# In a PyInstaller build __file__ points into a temporary extraction folder
# that is deleted on exit, so:
#   RES_DIR  -> bundled read-only assets (templates), from _MEIPASS
#   BASE_DIR -> the folder holding the .exe, where user data must live
FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RES_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    RES_DIR = BASE_DIR

DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
BIN_DIR = os.path.join(BASE_DIR, "bin")
for _d in (DOWNLOAD_DIR, BIN_DIR):
    try:
        os.makedirs(_d, exist_ok=True)
    except Exception:
        pass

# 下载目录可在网页菜单中修改，持久化到 download_dir.txt（重启后仍生效）
_DLDIR_CFG = os.path.join(BASE_DIR, "download_dir.txt")

def _load_download_dir():
    global DOWNLOAD_DIR
    try:
        with open(_DLDIR_CFG, "r", encoding="utf-8") as f:
            p = f.read().strip()
        if p and os.path.isdir(p):
            DOWNLOAD_DIR = p
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    except Exception:
        pass

_load_download_dir()

# yt-dlp 需要名为 ffmpeg 的可执行文件，把 imageio-ffmpeg 自带的拷贝过来
FFMPEG = os.path.join(BIN_DIR, "ffmpeg.exe")
if FROZEN:
    # The bundled ffmpeg already sits inside the package; copying it out to
    # bin/ would waste ~100 MB of disk on every launch.
    try:
        FFMPEG = get_ffmpeg_exe()
    except Exception:
        pass
elif not os.path.exists(FFMPEG):
    try:
        shutil.copy(get_ffmpeg_exe(), FFMPEG)
    except Exception:
        FFMPEG = get_ffmpeg_exe()

app = Flask(__name__, template_folder=os.path.join(RES_DIR, "templates"))
# Always re-read index.html when it changes; otherwise a long-running window
# keeps serving the version it cached at startup and edits look "ignored".
app.config["TEMPLATES_AUTO_RELOAD"] = True

# 下载任务表: task_id -> task dict
TASKS = {}
TASKS_LOCK = threading.Lock()

# 分辨率档位定义（高度 -> 标签）
RES_TIERS = [
    (4320, "8K"),
    (2160, "4K"),
    (1440, "2K"),
    (1080, "1080P"),
    (720, "720P"),
    (480, "480P"),
    (360, "360P"),
]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

# 图片类素材的扩展名
IMG_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "avif", "heic"}
VIDEO_EXTS = {"mp4", "m4v", "mov", "webm", "mkv", "flv", "ts", "avi", "wmv", "mpg", "mpeg", "3gp"}
AUDIO_EXTS = {"mp3", "m4a", "aac", "opus", "flac", "wav", "ogg", "wma"}


def patch_ytdlp_db_copy():
    """
    浏览器运行时会锁定 Cookie 数据库，yt-dlp 的普通复制会失败。
    降级方案：用 Windows 自带的 esentutl /y 复制被锁定的数据库文件，
    实现「浏览器开着也能读取 Cookie」，无需退出浏览器。
    """
    try:
        from yt_dlp import cookies as ydl_cookies
    except Exception:
        return
    orig = ydl_cookies._open_database_copy

    def patched(database_path, tmpdir):
        try:
            return orig(database_path, tmpdir)
        except PermissionError:
            dst = os.path.join(tmpdir, "temporary.sqlite")
            r = subprocess.run(["esentutl", "/y", database_path, "/d", dst, "/o"],
                               capture_output=True, text=True, timeout=60)
            if not os.path.exists(dst):
                raise PermissionError(
                    f"esentutl copy failed: {r.stdout[-200:] if r.stdout else ''} {r.stderr[-200:] if r.stderr else ''}")
            conn = sqlite3.connect(dst)
            return conn.cursor()

    ydl_cookies._open_database_copy = patched


patch_ytdlp_db_copy()


def sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name or "video")
    return name[:120].strip("._ ") or "video"


def title_out_dir(title: str, fallback: str) -> str:
    """
    每个链接一个文件夹，用内容标题命名（取不出标题时退回任务 id）。
    同一个链接的封面 / 视频 / 音频 / 图集都落在同一个文件夹里。
    """
    name = sanitize(title).strip(". ")[:80] or fallback
    d = os.path.join(DOWNLOAD_DIR, name)
    os.makedirs(d, exist_ok=True)
    return d


def height_label(h: int) -> str:
    for threshold, label in RES_TIERS:
        if h >= threshold:
            return label
    return f"{h}P"


def base_opts(proxy: str = "", cookie_browser: str = "") -> dict:
    if cookie_browser in ("auto", "none"):
        cookie_browser = ""  # 自动模式由 parse_attempts 决定，这里不直接读浏览器
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "socket_timeout": 20,
        "retries": 3,
        "ffmpeg_location": FFMPEG,
        "http_headers": {"User-Agent": UA},
        # yt-dlp 默认只找 deno 当 JS 运行时；把 node 也排上（装了 Node 没
        # 装 deno 的机器也能跑 YouTube 的挑战脚本，缺了会自动降级）。
        "js_runtimes": {"node": {}, "deno": {}},
    }
    if proxy and proxy != "direct":
        opts["proxy"] = proxy
    else:
        # 显式直连：避免系统环境变量里的代理（常已失效）干扰解析
        opts["proxy"] = ""
    if cookie_browser:
        cookie_file = os.path.join(BASE_DIR, f"cookies_{cookie_browser}.txt")
        if cookie_browser == "manual":
            # 用户通过「导入」上传的 Netscape cookies.txt（浏览器新版 App-Bound 加密下的推荐方案）
            if os.path.exists(cookie_file):
                opts["cookiefile"] = cookie_file
        elif os.path.exists(cookie_file):
            # 优先使用一次性导出的 Cookie 文件（浏览器可保持运行）
            opts["cookiefile"] = cookie_file
        else:
            # 实时从浏览器读取（要求浏览器完全退出）
            opts["cookiesfrombrowser"] = (cookie_browser, None, None, None)
    return opts


def _strip_query(u: str, names) -> str:
    """从 URL 查询串里移除指定参数（保留其余参数，避免破坏签名）"""
    base, sep, q = u.partition("?")
    if not sep:
        return u
    keep = [p for p in q.split("&") if p and p.split("=")[0] not in names]
    return base + ("?" + "&".join(keep) if keep else "")


def _is_watermarked(f: dict) -> bool:
    """
    判断某个流是不是「平台加过水印的版本」。
    常见特征：format_id/format_note 里带 watermark、水印，或 TikTok 这类平台的 download 版。
    """
    fid = str(f.get("format_id") or "").lower()
    note = str(f.get("format_note") or "").lower()
    desc = str(f.get("format") or "").lower()
    text = " ".join((fid, note, desc))
    if "watermark" in text or "水印" in text:
        return True
    if fid.startswith("download") or "download_addr" in fid or "download_addr" in note:
        return True
    return False


def _fmt_selector(height: int = 0) -> str:
    """
    下载格式选择：优先无水印流，其次同档位的任意流，最后兜底最佳。
    yt-dlp 的 [format_id!*=xxx] 过滤器在字段存在时生效；format_id 每个流都有，安全。
    """
    h = f"[height<={height}]" if height else ""
    nowm = "[format_id!*=watermark][format_id!*=download]"
    return (f"bestvideo{h}{nowm}+bestaudio/"
            f"best{h}{nowm}/"
            f"bestvideo{h}+bestaudio/best{h}/best")


def _original_image_url(url: str) -> str:
    """
    把各平台 CDN 的缩略图 / 压缩图地址改写为原图地址（尽力而为）。
    识别不了的返回原地址；下载失败时外层会自动回退到原地址。
    """
    u = (url or "").strip()
    if not u.startswith("http") or u.count("/") < 3:
        return url
    host = u.split("/")[2].lower()

    # X / Twitter：name=orig 就是上传原图
    if host.endswith("twimg.com"):
        if re.search(r"[?&]name=", u):
            return re.sub(r"([?&])name=[^&]*", r"\1name=orig", u)
        return u + ("&" if "?" in u else "?") + "name=orig"

    # 新浪微博图床：/thumb150/ /bmiddle/ /mw690/ /orj360/ -> /large/
    if host.endswith("sinaimg.cn"):
        return re.sub(r"/(thumb150|thumbnail|bmiddle|mw690|orj360|small|wap\d+|crop\.[^/]+)/",
                      "/large/", u)

    # 小红书：去掉 !h5_1080jpg 之类压缩后缀 + 图片处理参数
    if "xhscdn.com" in host or "xiaohongshu.com" in host:
        u = re.sub(r"![0-9A-Za-z_]+$", "", u)
        return _strip_query(u, {"imageView2", "imageMogr2", "x-oss-process"})

    # B 站：xxx.jpg@1280w_1e_1c.avif -> xxx.jpg
    if host.endswith("hdslb.com"):
        return re.sub(r"@[^/]*$", "", u)

    # 抖音 / 字节系：URL 带 URI 签名（x-signature 覆盖完整路径，含 ~tplv- 模板），
    # 一旦改写即 403，带签名时必须原样返回；无签名的才可去掉处理模板取原图
    if any(h in host for h in ("douyinpic.com", "byteimg.com", "ixigua.com", "bytedance.com")):
        if re.search(r"[?&]x-signature=", u) or "signature=" in u.split("?")[-1]:
            return u
        return re.sub(r"~tplv-[^?]*", "", u)

    # 通用：去掉常见的缩放 / 压缩处理参数
    return _strip_query(u, {"x-oss-process", "imageView2", "imageMogr2",
                            "imageslim", "resize", "w", "h", "quality"})


def _is_image_format(f: dict) -> bool:
    """判断某个 format 是否其实是一张图片（部分平台的图集帖以 format 形式返回）"""
    ext = (f.get("ext") or "").lower()
    if ext not in IMG_EXTS:
        return False
    return f.get("vcodec") in (None, "none") and f.get("acodec") in (None, "none")


def _largest_thumb(entry: dict):
    """取该条目尺寸最大的缩略图（图集帖没有 formats 时用它兜底）"""
    thumbs = entry.get("thumbnails") or []
    if not thumbs:
        return []
    best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0)
               or (t.get("preference") or 0))
    url = best.get("url")
    return [(url, "jpg")] if url else []


def _dedup_urls(urls):
    """去重保序，过滤空值"""
    out, seen = [], set()
    for u in urls:
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _best_cover(info: dict) -> str:
    """
    挑选尺寸最大的封面。
    很多平台的 info["thumbnail"] 只是 640x360 级别的默认小图（如 YouTube hqdefault），
    必须遍历 thumbnails 按分辨率取最大；无尺寸信息时取最后一项（通常更大）。
    播放列表取首个有缩略图的条目。
    """
    def _score(t):
        w, h = t.get("width") or 0, t.get("height") or 0
        if not (w and h):
            m = re.match(r"(\d+)x(\d+)", t.get("resolution") or "")
            if m:
                w, h = int(m.group(1)), int(m.group(2))
        return (w * h, t.get("preference") or 0)

    entries = [e for e in (info.get("entries") or []) if e] or [info]
    for e in entries:
        thumbs = e.get("thumbnails") or []
        if not thumbs:
            continue
        best = max(thumbs, key=_score)
        if _score(best) > (0, 0):
            url = best.get("url") or ""
            if url:
                return url
        url = thumbs[-1].get("url") or ""
        if url:
            return url
    for e in entries:
        if e.get("thumbnail"):
            return e["thumbnail"]
    return info.get("thumbnail") or ""


def _collect_images(info: dict):
    """从解析结果中收集图集图片，兼容单图帖与 carousel / playlist 多图帖"""
    entries = [e for e in (info.get("entries") or []) if e]
    if not entries:
        entries = [info]
    images, seen = [], set()
    for e in entries:
        cands = []
        for f in e.get("formats") or []:
            if _is_image_format(f):
                cands.append((f.get("url"), (f.get("ext") or "jpg").lower()))
        if not cands:
            u = e.get("url") or ""
            ext = (e.get("ext") or "").lower()
            if u and ext in IMG_EXTS:
                cands.append((u, ext))
        # 纯图片帖通常既没有 formats 也没有时长，此时用最大缩略图兜底
        if not cands and not (e.get("formats") or e.get("height") or e.get("duration")):
            cands += _largest_thumb(e)
        for u, ext in cands:
            if u and u not in seen:
                seen.add(u)
                images.append({"url": _original_image_url(u), "src": u,
                               "ext": ext if ext in IMG_EXTS else "jpg"})
    return images


def _has_video(info: dict) -> bool:
    """
    是否存在真正的视频流。
    注意：不能只看 height/width——很多平台的纯图片帖（Instagram、图集帖）也带宽高，
    那样会被误判成视频。判据按强弱：vcodec 明确有值 > 视频扩展名+有时长 > formats 里的视频流。
    """
    ext = (info.get("ext") or "").lower()
    vcodec = str(info.get("vcodec") or "").lower()
    if vcodec not in ("", "none"):
        return True
    if ext in VIDEO_EXTS and (info.get("duration") or info.get("formats")):
        return True
    for f in info.get("formats") or []:
        fv = str(f.get("vcodec") or "").lower()
        fext = (f.get("ext") or "").lower()
        if fv not in ("", "none"):
            return True
        # 编码未知但扩展名/协议是视频（如 X/Twitter 的渐进式 mp4）
        if fext in VIDEO_EXTS and not (f.get("acodec") and fext in AUDIO_EXTS):
            if (f.get("height") or f.get("width") or f.get("vbr") or f.get("fps")
                    or str(f.get("protocol") or "").startswith("m3u8")
                    or str(f.get("protocol") or "").startswith("dash")):
                return True
    for e in info.get("entries") or []:
        if e and _has_video(e):
            return True
    return False


def _has_audio(info: dict) -> bool:
    """是否存在音频流（音乐平台常见：只有音频没有视频）"""
    for f in info.get("formats") or []:
        if f.get("acodec") not in (None, "none"):
            return True
    for e in info.get("entries") or []:
        if e and _has_audio(e):
            return True
    return bool(info.get("ext") in ("mp3", "m4a", "opus", "flac", "wav", "ogg"))


def _cookie_sources(proxy: str = ""):
    """返回本机可用的 Cookie 来源 [(标签, opts)]，仅在有需要时才尝试"""
    cands = []
    # 已导出的 Cookie 文件最稳（不需要关闭浏览器）
    for label in ("manual", "edge", "chrome", "firefox"):
        f = os.path.join(BASE_DIR, f"cookies_{label}.txt")
        if os.path.exists(f) and os.path.getsize(f) > 80:
            o = base_opts(proxy, "")
            o["cookiefile"] = f
            cands.append((label, o))
    # 浏览器实时读取（浏览器正在运行会失败的会被自动跳过）
    for b in ("edge", "chrome", "firefox"):
        o = base_opts(proxy, "")
        o["cookiesfrombrowser"] = (b, None, None, None)
        cands.append((b, o))
    return cands


def parse_attempts(proxy: str = "", cookie_browser: str = ""):
    """
    自动模式：先「无 Cookie 直解」——绝大多数平台根本不需要登录；
    只有遇到「需要登录」类错误，才逐个尝试本机浏览器的登录态。
    """
    if cookie_browser and cookie_browser != "auto":
        if cookie_browser == "manual":
            f = os.path.join(BASE_DIR, "cookies_manual.txt")
            o = base_opts(proxy, "")
            if os.path.exists(f):
                o["cookiefile"] = f
            return [("manual", o)]
        return [(cookie_browser, base_opts(proxy, cookie_browser))]
    return [("", base_opts(proxy, ""))] + _cookie_sources(proxy)


def _pick_preview(info: dict, kind: str = "video"):
    """
    挑一个浏览器能直接播的地址做在线预览（video 用 <video>，audio 用 <audio>）：
    视频优先「视频+音频」合并格式，其次纯视频流（此时预览无声），
    排除 m3u8/DASH 分片流（浏览器原生不支持）。
    注意：部分平台（如 X/Twitter）的渐进式 mp4 不填 vcodec/acodec（None），
    不能把 None 当作「无」，只把显式的 "none" 当作无。
    返回 (url, silent, audio_url)
    """
    direct = info.get("url")
    if (direct and str(direct).startswith("http") and ".m3u8" not in str(direct).split("?")[0]
            and not _is_watermarked(info)):
        if kind == "audio":
            if info.get("vcodec", "x") == "none":
                return direct, False, ""
        elif info.get("vcodec", "x") != "none":
            return direct, False, ""
    fmts = [f for f in info.get("formats") or [] if f.get("url")]

    def playable(f):
        u = f.get("url") or ""
        proto = (f.get("protocol") or "")
        return (u.startswith("http")
                and not proto.startswith(("m3u8", "dash"))
                and ".m3u8" not in u.split("?")[0])

    def best(cands, cap=1080):
        # 去水印优先：同一档位里绝不挑带水印的流
        clean = [f for f in cands if not _is_watermarked(f)]
        if clean:
            cands = clean
        under = [f for f in cands if (f.get("height") or 0) <= cap] or cands
        if not under:
            return None
        return max(under, key=lambda f: (f.get("height") or 0, f.get("tbr") or f.get("abr") or 0))

    if kind == "audio":
        # 纯音频流：m4a / mp3 / webm 都能被 <audio> 直接播；
        # 编码未知的 https 直链也兜底尝试（很多音频站不填 codec 字段）
        aud = [f for f in fmts if playable(f)
               and f.get("vcodec") == "none" and f.get("acodec") != "none"]
        unk = [f for f in fmts if playable(f)
               and f.get("vcodec") is None and f.get("acodec") is None]
        pool = aud or unk
        f = max(pool, key=lambda f: (f.get("abr") or f.get("tbr") or 0)) if pool else None
        return (f["url"], False, "") if f else ("", False, "")

    # 视频：https 直链且带音频（含 codec 未知的渐进式 mp4，如 X/Twitter）
    def usable(f):
        return playable(f) and f.get("vcodec") != "none"

    def with_audio(f):
        return f.get("acodec") not in (None, "none")

    def unknown_codecs(f):
        return f.get("vcodec") is None and f.get("acodec") is None

    prog = [f for f in fmts if usable(f) and (with_audio(f) or unknown_codecs(f))]
    vid = [f for f in fmts if usable(f)]
    f = best(prog) or best(vid)
    if not f:
        return "", False, ""
    silent = f.get("acodec") == "none"      # 只有明确标注无音频才算无声
    audio_url = ""
    if silent:
        # B站/YouTube 等 DASH 平台音视频分离：额外给一条音频流，前端与画面同步播放
        aud = [x for x in fmts if playable(x)
               and x.get("vcodec") == "none" and x.get("acodec") != "none"]
        if aud:
            audio_url = max(aud, key=lambda x: (x.get("abr") or x.get("tbr") or 0))["url"]
    return f["url"], silent, audio_url


# ---------- 歌词（LRC） ----------
_NETEASE_ID_RE = re.compile(r"music\.163\.com.*?[?&]id=(\d+)")

def _netease_lyrics(url: str):
    """网易云歌曲的歌词：返回 (LRC 原文, LRC 翻译)；非网易云或失败返回 ("", "")"""
    m = _NETEASE_ID_RE.search(url or "")
    if not m:
        return "", ""
    try:
        import urllib.request as _ur
        req = _ur.Request(
            f"https://music.163.com/api/song/lyric?id={m.group(1)}&lv=1&kv=1&tv=-1",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"})
        with _ur.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return ((data.get("lrc") or {}).get("lyric") or "",
                (data.get("tlyric") or {}).get("lyric") or "")
    except Exception:
        return "", ""

_LRC_TS_RE = re.compile(r"^\[((?:\d+:)?\d+:\d+(?:[.:]\d+)?)\]")

def _lrc_merge(trans_src: str, trans: str) -> str:
    """把翻译 LRC 按时间戳合并进原文，输出双语 LRC"""
    if not trans:
        return trans_src
    tmap = {}
    for line in trans.splitlines():
        m = _LRC_TS_RE.match(line.strip())
        if m:
            try:
                parts = [float(x) for x in m.group(1).replace(":", " ").split()]
                sec = parts[0] * 60 + parts[1] + (parts[2] if len(parts) > 2 else 0)
                text = _LRC_TS_RE.sub("", line.strip()).strip()
                if text:
                    tmap[round(sec, 1)] = text
            except (ValueError, IndexError):
                continue
    if not tmap:
        return trans_src
    out = []
    for line in trans_src.splitlines():
        out.append(line)
        m = _LRC_TS_RE.match(line.strip())
        if m:
            try:
                parts = [float(x) for x in m.group(1).replace(":", " ").split()]
                sec = parts[0] * 60 + parts[1] + (parts[2] if len(parts) > 2 else 0)
                t = tmap.get(round(sec, 1))
                if t:
                    out.append(t)
            except (ValueError, IndexError):
                continue
    return "\n".join(out)

def _embed_lyrics(mp3_path: str, lrc: str, trans: str) -> bool:
    """把歌词写入 MP3 的 USLT 歌词帧（带时间戳，支持滚动歌词的播放器可直接显示）"""
    try:
        from mutagen.id3 import ID3, USLT
        tags = ID3(mp3_path)
        tags.setall("USLT", [])
        tags.add(USLT(encoding=3, lang="chi" if trans else "eng",
                      desc="", text=_lrc_merge(lrc, trans)))
        tags.save()
        return True
    except Exception:
        return False


# ---------------- 汽水音乐分享链接专用通道（yt-dlp 不认 qishui.douyin.com） ----------------
_QISHUI_URL_RE = re.compile(r"qishui\.douyin\.com/|music\.douyin\.com/qishui/", re.I)


def _qishui_page_data(url: str):
    """拉取汽水音乐分享落地页，解析 _ROUTER_DATA 初始数据；失败返回 None"""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read(8 << 20).decode("utf-8", "replace")
        j = body.find("_ROUTER_DATA = {")
        if j < 0:
            return None
        obj, _ = json.JSONDecoder().raw_decode(body[j + 15:])
        return ((obj.get("loaderData") or {}).get("track_page") or {}
                ).get("audioWithLyricsOption") or None
    except Exception:
        return None


def _qishui_lrc(lyrics_obj) -> str:
    """汽水歌词结构（lyrics.sentences: startMs/endMs/text）转 LRC"""
    if not isinstance(lyrics_obj, dict):
        return ""
    out = []
    for it in lyrics_obj.get("sentences") or []:
        if not isinstance(it, dict) or not (it.get("text") or "").strip():
            continue
        sec = (it.get("startMs") or 0) / 1000.0
        m, s = divmod(sec, 60)
        out.append(f"[{int(m):02d}:{s:05.2f}]{it['text'].strip()}")
    return "\n".join(out)


def _qishui_analyze(url: str):
    """汽水音乐分享链接（qishui.douyin.com 短链 / music.douyin.com/qishui 落地页）。
    返回与 analyze_url 同构的 audio 结果；非汽水链接返回 None；
    平台未下发音频源（版权调整/会员限制）时抛出带明确原因的错误"""
    if not _QISHUI_URL_RE.search(url or ""):
        return None
    aw = _qishui_page_data(url)
    if not aw:
        raise RuntimeError("qishui_parse 汽水音乐页面数据获取失败，请确认分享链接有效后重试")
    audio_url = (aw.get("url") or "").strip()
    if not audio_url:
        hint = ((aw.get("trackInfo") or {}).get("display_hint") or {}).get("view_only_msg") or ""
        raise RuntimeError(
            "qishui_no_audio 平台未提供该歌曲的音频源，任何工具都无法下载："
            + (hint or "该音源因版权调整暂不可用，或下载需要平台会员")
            + "（可在汽水音乐 App 里确认这首歌是否还能播放）")
    dur = int(float(aw.get("duration") or 0))
    m, s = divmod(dur, 60)
    cover = aw.get("coverURL") or ""
    return {
        "title": aw.get("trackName") or "未知标题",
        "author": aw.get("artistName") or "未知作者",
        "lyrics": _qishui_lrc(aw.get("lyrics")),
        "lyrics_trans": "",
        "cover": cover,
        "covers": ([cover] if cover else []),
        "duration": f"{m}:{s:02d}" if dur else "--:--",
        "platform": "QISHUI",
        "webpage": url,
        "resolutions": [],
        "views": None,
        "is_live": False,
        "content_type": "audio",
        "image_count": 0,
        "images": [],
        "ext": "mp3",
        "preview_url": audio_url,
        "preview_silent": False,
        "preview_audio_url": audio_url,
        "cookie_used": "qishui",
    }


# ------------- 影视聚合站专用通道（电影先生等苹果CMS架构站） -------------
# 这类站点（苹果CMS 内核，模板大同小异）详情页本身没有视频：详情页列出多条播放
# 线路（/play/影片ID-线路-集数.html 等），播放页内嵌播放器的 player_aaaa 配置里
# 才是真正的 m3u8 视频源
_FILM_SITES = {
    "silidm.com": "电影先生",
}
_FILM_URL_RE = re.compile(
    r"https?://([A-Za-z0-9.-]+)/(?:video/(\d+)\.html|play/[^?\s]+\.html|vodplay/[^?\s]+\.html)",
    re.I)
# 同类站自动识别：URL 路径长得像影视站的详情页/播放页（泛化支持，不逐一收录域名）。
# 路径段不要求标准名：很多站用自定义前缀（如 dsxyyplay / dsxyydetail），只要求"含"关键字
_FILM_PATH_RE = re.compile(
    r"/[a-z0-9_-]{0,16}?(?:play|vod|detail|video)[a-z0-9_-]{0,16}/[^?\s#/]*?\d", re.I)
# 影视站形态识别会误伤的主流平台（有自己的专用通道/yt-dlp 支持），不做泛化尝试
_FILM_GENERIC_SKIP = (
    "bilibili.com", "youtube.com", "youtu.be", "douyin.com", "iesdouyin.com",
    "ixigua.com", "kuaishou.com", "weibo.com", "weibo.cn", "xiaohongshu.com",
    "xhslink.com", "twitter.com", "x.com", "iqiyi.com", "iq.com", "qq.com",
    "youku.com", "baidu.com", "tiktok.com", "instagram.com",
    "facebook.com", "twitch.tv", "soundcloud.com", "netflix.com")


def _film_site(url: str):
    """命中已收录的影视站时返回 (站点主页, 站点名)，否则 None"""
    m = _FILM_URL_RE.search(url or "")
    if not m:
        return None
    host = m.group(1).lower()
    for domain, name in _FILM_SITES.items():
        if host == domain or host.endswith("." + domain):
            return ("https://" + host, name)
    return None


def _film_looks_like(url: str):
    """URL 是否像未收录的同类影视站（用于泛化尝试；命中主流平台则排除）"""
    if not _FILM_PATH_RE.search(url or ""):
        return False
    host = (re.match(r"https?://([^/?#]+)", url or "", re.I) or [None, ""])[1].lower()
    return not any(host == d or host.endswith("." + d)
                   for d in _FILM_GENERIC_SKIP if "/" not in d)


_FILM_HTML_CACHE = {}                     # url -> (时间戳, html)：解析/选集反复抓同页时免重复请求
_FILM_HTML_CACHE_TTL = 120                # 秒；影视站页面短时间内不会变


def _film_html(url: str) -> str:
    now = time.time()
    hit = _FILM_HTML_CACHE.get(url)
    if hit and now - hit[0] < _FILM_HTML_CACHE_TTL:
        return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": url})
    # 国内漫画站直连优先：系统代理（Clash 规则/上游波动）经常对它 502，
    # 直连失败再退回系统代理试一次；影视站维持原样（跟随系统代理）
    if _manga_site(url):
        openers = [urllib.request.build_opener(urllib.request.ProxyHandler({})),
                   urllib.request.build_opener()]
    else:
        openers = [urllib.request.build_opener()]
    html, last = None, None
    for op in openers:
        try:
            with op.open(req, timeout=25) as r:
                html = r.read().decode("utf-8", "ignore")
            break
        except Exception as e:
            last = e
    if html is None:
        raise last if last else RuntimeError("fetch failed")
    if len(_FILM_HTML_CACHE) > 64:        # 防膨胀：丢最早条目
        _FILM_HTML_CACHE.pop(next(iter(_FILM_HTML_CACHE)))
    _FILM_HTML_CACHE[url] = (now, html)
    return html


def _film_episode_groups(html: str):
    """详情页 -> 按线路分组的完整剧集列表。
    返回 [(线路key, [(集label, 播放页链接), ...]), ...]，线路按页面出现顺序，集按集数升序。
    播放页路径段不要求标准名（dsxyyplay 等自定义前缀也认）"""
    groups = {}                           # 线路key -> {ep序号: (label, url)}
    for m in re.finditer(
            r'href="([^"]*?/[a-z0-9_-]*play[a-z0-9_-]*/[^"?]+\.html)"[^>]*>([^<]*)</a>',
            html, re.I):
        u, text = m.group(1), m.group(2).strip()
        me = re.search(r"-(\d+)\.html$", u)
        ep = int(me.group(1)) if me else 0
        key = re.sub(r"-\d+\.html$", ".html", u)   # 去掉结尾集数 = 线路
        groups.setdefault(key, {})[ep] = (text, u)
    routes = []
    for key, eps in groups.items():
        items = [(ep, label, u) for ep, (label, u) in eps.items()]
        items.sort(key=lambda x: x[0])
        routes.append((key, items))
    return routes


def _ep_label(ep: int, text: str) -> str:
    """集数标签：优先用页面文字（如「第3集」「预告片」），纯数字时规范化为「第N集」"""
    text = (text or "").strip()
    if text and not re.fullmatch(r"\d+", text):
        return text[:24]
    return f"第{ep}集"


def _film_m3u8(play_url: str) -> str:
    """播放页 -> 播放器配置（player_aaaa / player_data）里的视频源地址。
    配置里可能嵌套子对象（如 vod_data），非贪婪正则会截断 JSON，改用花括号计数取完整对象"""
    html = _film_html(play_url)
    for key in ("player_aaaa", "player_data"):
        m = re.search(key + r"\s*=\s*", html)
        if not m:
            continue
        i = html.find("{", m.end())
        if i < 0:
            continue
        depth, end = 0, -1
        for j in range(i, len(html)):
            if html[j] == "{":
                depth += 1
            elif html[j] == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end < 0:
            continue
        try:
            cfg = json.loads(html[i:end + 1])
        except ValueError:
            continue
        u = (cfg.get("url") or "").strip()
        if u:
            # 个别线路用 $$$/### 分隔多清晰度或多片段，取第一段
            return u.split("$$$")[0].split("###")[0]
    return ""


def _film_title_cover(html: str):
    """详情页/播放页 HTML -> (片名, 封面)。
    片名：《片名》 -> og:title -> title 去站名后缀；
    封面：alt 恰为片名的 <img> -> og:image -> 第一张 upload/vod 图"""
    mt = re.search(r"<title>[^<]*《(.+?)》", html) or re.search(r"《(.+?)》", html)
    title = mt.group(1).strip() if mt else ""
    if not title:                    # 同类站片名格式各异：og:title / 标题去站名后缀
        mo = (re.search(r'property="og:title"[^>]+content="([^"]+)"', html)
              or re.search(r"<title>([^<]+)", html))
        title = re.split(r"\s+[-_–]\s+", mo.group(1).strip())[0].strip() if mo else ""
    cover = ""
    if title:
        for tag in re.findall(r"<img\b[^>]*>", html):
            ma = re.search(r'alt="([^"]*)"', tag)
            if not ma or ma.group(1).strip() != title:
                continue
            for attr in ("data-src", "src"):
                mu = re.search(attr + r'="([^"]+)"', tag)
                if mu and mu.group(1).startswith("http") \
                        and "loading" not in mu.group(1):
                    cover = mu.group(1)
                    break
            if cover:
                break
    if not cover:                     # 兜底：og:image
        mc = re.search(r'property="og:image"[^>]+content="([^"]+)"', html)
        cover = mc.group(1) if mc else ""
    if not cover:
        mc = re.search(r'(https?://[^"\'\s]+/upload/vod/[^"\'\s]+\.(?:webp|jpe?g|png))',
                       html)
        cover = mc.group(1) if mc else ""
    return title, cover


def _film_resolve(url: str, generic: bool = False, play_url: str = ""):
    """影视站链接 -> (m3u8, 标题, 封面, 站点主页, 站点名, 剧集列表)；解析不到就抛 film_parse 错误。
    generic=True 时接受任意同类站（苹果CMS 内核自动识别），站点名用域名。
    play_url：详情页解析时指定某集的播放页链接（用户在面板上选了集数），为空则自动选可用线路的第一集。
    剧集列表为 [{"label": "第1集", "url": 播放页链接}, ...]（单集或无法分组时为 []）"""
    site = _film_site(url)
    if site:
        origin, name = site
    elif generic:
        mh = re.match(r"https?://([^/?#]+)", url or "", re.I)
        if not mh:
            raise RuntimeError("film_parse 链接格式有误")
        host = mh.group(1).lower()
        origin = "https://" + host
        name = re.sub(r"^www\.", "", host)
    else:
        raise RuntimeError("film_parse 链接不属于已收录的影视站")
    title, cover = "", ""
    episodes = []                          # 当前线路的完整剧集（前端选集用）
    if re.search(r"/[a-z0-9_-]*play[a-z0-9_-]*/[^?\s]+\.html", url, re.I):
        pages = [play_url or url]
        ptitle = pcover = ""
        try:                               # 播放页通常也有 og:title，补出文件名
            phtml = _film_html(pages[0])
            ptitle, pcover = _film_title_cover(phtml)
        except Exception:
            phtml = ""
        # 从播放页反查详情页：带回所在线路的完整剧集列表 + 更准的片名/封面
        # （详情页路径段也是自定义的，dsxyydetail 等；优先取与播放页同 ID 的那条，防抓到推荐位）
        if phtml:
            try:
                mid = re.search(r"/(\d+)(?:-\d+)*\.html", pages[0])
                md = None
                cands = re.findall(
                    r'href="([^"]*?/[a-z0-9_-]*(?:detail|video|vod)[a-z0-9_-]*/\d+\.html)"',
                    phtml, re.I)
                if mid:
                    md = next((c for c in cands
                               if re.search(r"/" + mid.group(1) + r"\.html$", c)), None)
                if md is None and cands:
                    md = cands[0]
                if md:
                    durl = md
                    if not durl.startswith("http"):
                        durl = origin + durl
                    dhtml = _film_html(durl)
                    dtitle, dcover = _film_title_cover(dhtml)
                    title = dtitle or ptitle
                    cover = dcover or pcover
                    for _key, items in _film_episode_groups(dhtml):
                        if any((u if u.startswith("http") else origin + u) == pages[0]
                               for _ep, _lb, u in items):
                            episodes = [{"label": _ep_label(ep, lb),
                                         "url": u if u.startswith("http") else origin + u}
                                        for ep, lb, u in items]
                            break
            except Exception:
                pass
        title, cover = title or ptitle, cover or pcover
    else:
        html = _film_html(url)
        title, cover = _film_title_cover(html)
        # 按线路分组剧集；指定了 play_url 就锁定该集所在线路，否则逐条线路尝试
        routes = _film_episode_groups(html)
        if play_url:
            for _key, items in routes:
                if any((u if u.startswith("http") else origin + u) == play_url
                       for _ep, _lb, u in items):
                    episodes = [{"label": _ep_label(ep, lb),
                                 "url": u if u.startswith("http") else origin + u}
                                for ep, lb, u in items]
                    break
            pages = [play_url]
        else:
            pages = []
            for _key, items in routes:    # 每条线路的第一集试解析，直到拿到可用源
                pages = [u if u.startswith("http") else origin + u
                         for _ep, _lb, u in items[:1]]
                if not pages:
                    continue
                try:
                    if _film_m3u8(pages[0]):
                        episodes = [{"label": _ep_label(ep, lb),
                                     "url": u if u.startswith("http") else origin + u}
                                    for ep, lb, u in items]
                        break
                except Exception:
                    pass
                pages = []
        if not pages:
            raise RuntimeError(
                "film_parse 该影片页面里没有播放线路：请确认链接是影片详情页/播放页"
                "（浏览器里能看到「播放」按钮的页面）")
    m3u8 = ""
    for p in pages:                       # 多条线路逐条尝试，直到拿到可用的 m3u8
        try:
            m3u8 = _film_m3u8(p)
        except Exception:
            m3u8 = ""
        if m3u8:
            break
    if not m3u8:
        raise RuntimeError(
            "film_parse 播放线路里没有找到可解析的视频源（线路可能已失效，"
            "请在原站换一条线路后重试）")
    return m3u8, title, cover, origin, name, episodes


def _film_analyze(url: str):
    """影视站解析：详情页/播放页 -> m3u8 -> yt-dlp 取时长画质等元数据。
    已收录站失败时报明确错误；未收录同类站泛化尝试、失败则回落通用解析（返回 None）"""
    generic = False
    if not _film_site(url):
        if not _film_looks_like(url):
            return None
        generic = True
        try:
            return _film_result(url, generic=True)
        except Exception:
            return None                # 不是同类站/该站结构不同 -> 交给通用解析
    return _film_result(url)


def _douban_poster(title: str) -> str:
    """按片名到豆瓣找高清海报（原图 480x600 级别，远大于影视站自带的小图）。
    s_ratio_poster 只是缩略图，把路径换成 photo/ 即原图；失败返回空串"""
    if not title:
        return ""
    try:
        from urllib.parse import quote
        req = urllib.request.Request(
            "https://movie.douban.com/j/subject_suggest?q=" + quote(title),
            headers={"User-Agent": UA, "Referer": "https://movie.douban.com/"})
        with urllib.request.urlopen(req, timeout=10) as r:
            arr = json.loads(r.read().decode("utf-8", "ignore"))
        for it in arr or []:
            img = (it.get("img") or "").strip()
            if img and (it.get("title") or "").strip() == title:
                return img.replace("/s_ratio_poster/", "/photo/")
    except Exception:
        pass
    return ""


def _film_meta(m3u8_url: str, origin: str):
    """轻量 HLS 元数据：直接解析 master/media 播放列表拿时长和最高分辨率。
    比 yt-dlp extract_info 快数倍（少拉一层清单、无额外探测请求）；
    结构特殊解析不了会抛异常，由调用方回落 yt-dlp。返回 (时长秒, 分辨率高度集合)"""
    from urllib.parse import urljoin

    def fetch(u: str) -> str:
        req = urllib.request.Request(
            u, headers={"User-Agent": UA, "Referer": origin + "/"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", "ignore")

    text = fetch(m3u8_url)
    heights = set()
    media_url = ""
    if "#EXT-X-STREAM-INF" in text:            # master 清单：取最高码率的子流
        best_key, best_uri = (-1, -1), ""
        for m in re.finditer(r"#EXT-X-STREAM-INF:([^\n]+)\n([^\n#][^\n]*)", text):
            attrs = m.group(1)
            mr = re.search(r"RESOLUTION=(?:\d+x)?(\d+)", attrs)
            mb = re.search(r"BANDWIDTH=(\d+)", attrs)
            key = (int(mr.group(1)) if mr else 0, int(mb.group(1)) if mb else 0)
            if key > best_key:
                best_key, best_uri = key, m.group(2).strip()
        if not best_uri:
            raise ValueError("no variant")
        if best_key[0]:
            heights.add(best_key[0])
        media_url = urljoin(m3u8_url, best_uri)
        text = fetch(media_url)                # media 清单：数 EXTINF 求总时长
    dur = 0.0
    for m in re.finditer(r"#EXTINF:([\d.]+)", text):
        dur += float(m.group(1))
    if dur <= 0 and not heights:
        raise ValueError("empty playlist")
    return int(round(dur)), heights


def _film_result(url: str, generic: bool = False):
    m3u8, title, cover, origin, name, episodes = _film_resolve(url, generic=generic)
    # 豆瓣海报与视频元数据互不依赖，并行取，省一次串行等待
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_douban = ex.submit(_douban_poster, title)
        f_meta = ex.submit(_film_meta, m3u8, origin)
        try:
            dur, heights = f_meta.result()
        except Exception:
            # 轻量解析失败（清单结构特殊等）：回落 yt-dlp 完整元数据
            opts = base_opts()
            opts["http_headers"] = dict(opts.get("http_headers") or {}, Referer=origin + "/")
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(m3u8, download=False)
            except Exception as e:
                raise RuntimeError("film_source 视频源拉取失败（影片服务器暂时无法访问）："
                                   + str(e)[:120])
            heights = {int(f["height"]) for f in (info.get("formats") or [])
                       if f.get("height")}
            if info.get("height"):
                heights.add(int(info["height"]))
            dur = int(info.get("duration") or 0)
        douban = f_douban.result()
    covers = [u for u in (douban, cover) if u][:6]
    cover = douban or cover              # 豆瓣原图优先，站内小图兜底
    tiers = []
    for threshold, label in RES_TIERS:
        if any(h >= threshold for h in heights):
            tiers.append(label)
    if not tiers and heights:
        tiers = [height_label(max(heights))]
    tiers = tiers[::-1] or ["原画"]       # HLS 通常只有单一画质流，即原片画质
    h, m, s = dur // 3600, dur % 3600 // 60, dur % 60
    duration_text = f"{h}:{m:02d}:{s:02d}" if h else (f"{m}:{s:02d}" if dur else "--:--")
    return {
        "title": title or name,
        "author": name,
        "lyrics": "",
        "lyrics_trans": "",
        "cover": cover,
        "covers": covers,
        "duration": duration_text,
        "platform": name,
        "webpage": url,
        "resolutions": tiers,
        "views": None,
        "is_live": False,
        "content_type": "video",
        "image_count": 0,
        "images": [],
        "ext": "mp4",
        # 多集影片：完整剧集列表（前端下拉选集，下载时把所选集的播放页带回）
        "episodes": episodes if len(episodes) > 1 else [],
        # m3u8 分片流：前端用 hls.js 经本地代理预览（见 api_video_preview）
        "preview_url": m3u8,
        "preview_silent": False,
        "preview_audio_url": "",
        "cookie_used": "",
    }


def _film_download(task_id: str, url: str, play_url: str = "",
                   parent_dir: str = "", ep_label: str = "", quiet: bool = False):
    """影视站下载：解析出 m3u8 后交给 yt-dlp（HLS 分片自动合成为 MP4）。
    play_url：用户在面板上选定的某集播放页链接（空 = 默认线路第一集）。
    parent_dir/ep_label/quiet：批量下载全集时提供——mp4 归档到
    总文件夹/{集名}/ 二级子文件夹，任务终态由批量调用方负责"""
    task = TASKS[task_id]
    out_dir = os.path.join(DOWNLOAD_DIR, task_id)
    os.makedirs(out_dir, exist_ok=True)
    m3u8, title, _cover, origin, _name, eps = _film_resolve(
        url, generic=not _film_site(url), play_url=play_url)
    title = sanitize(title or "film")
    # 多集影片：文件名带上集数（片名 第N集.mp4），不同集不会互相覆盖
    if play_url:
        label = ""
        for it in (eps or []):
            if it.get("url") == play_url:
                label = it.get("label") or ""
                break
        if not label:
            me = re.search(r"-(\d+)\.html$", play_url)
            if me and (len(eps or []) > 1 or not eps):
                label = f"第{int(me.group(1))}集"
        if label and len(eps or []) != 1:
            title = sanitize(f"{title} {label}")
    task.update({"status": "downloading", "progress": 2})
    opts = base_opts()
    opts["http_headers"] = dict(opts.get("http_headers") or {}, Referer=origin + "/")
    opts.update({
        "format": "best",
        "outtmpl": os.path.join(out_dir, title + ".%(ext)s"),
        "merge_output_format": "mp4",
        # 并发拉分片提速（豆瓣等图站/影片服务器按连接数限速，并发越多越快）
        "concurrent_fragment_downloads": 16,
        "progress_hooks": [lambda d: progress_hook(task_id, d)],
        "postprocessor_hooks": [lambda d: pp_hook(task_id, d)],
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([m3u8])
    files = [os.path.join(out_dir, f) for f in os.listdir(out_dir)]
    files = [f for f in files if not f.endswith(".part")]
    if not files:
        raise RuntimeError("未找到下载产物")
    filepath = max(files, key=os.path.getsize)
    # 归档：单集按片名归入同一链接的文件夹；批量归入 总文件夹/{集名}/
    try:
        dest_dir = (os.path.join(parent_dir, sanitize(ep_label or "第0集"))
                    if parent_dir else title_out_dir(title, task_id))
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, os.path.basename(filepath))
        if os.path.abspath(dest) != os.path.abspath(filepath):
            if os.path.exists(dest):
                try:
                    os.remove(dest)
                except OSError:
                    pass
            shutil.move(filepath, dest)
            filepath = dest
            try:
                if not os.listdir(out_dir):
                    os.rmdir(out_dir)
            except OSError:
                pass
    except OSError:
        pass
    if quiet:
        return filepath
    task.update({"status": "done", "progress": 100,
                 "filepath": filepath, "filename": os.path.basename(filepath)})


def _film_download_batch(task_id: str, url: str):
    """影视站批量下载全集：每集依次下载，mp4 归档到 总文件夹/{集名}/ 二级子文件夹；
    某集失败跳过并继续，最后汇总成功数"""
    task = TASKS[task_id]
    try:
        _m, title, _c, _o, _n, eps = _film_resolve(
            url, generic=not _film_site(url))
        eps = eps or []
        if len(eps) < 2:
            raise RuntimeError("未解析出多集列表，无法批量下载")
        parent = title_out_dir(sanitize(title or "film"), task_id)
        total = len(eps)
        fails = []
        for idx, ep in enumerate(eps, 1):
            label = ep.get("label") or f"第{idx}集"
            task.update({"status": "downloading",
                         "count": f"{idx}/{total} 集",
                         "episode": label,
                         "progress": int((idx - 1) * 100 / total)})
            try:
                _film_download(task_id, url, play_url=ep["url"],
                               parent_dir=parent, ep_label=label, quiet=True)
            except Exception:
                fails.append(label)
        task.update({"status": "done", "progress": 100,
                     "count": f"{total - len(fails)}/{total} 集",
                     "filepath": parent,
                     "filename": os.path.basename(parent),
                     "failed": fails})
    except Exception as e:
        code = classify_error(str(e))
        task.update({"status": "error",
                     "error": ZH_ERRORS.get(code, "下载失败"),
                     "detail": str(e)[:300],
                     "error_code": code})


def _short_reason(err: str) -> str:
    """把内部异常压成一句人话，用于批量结果里的失败原因"""
    e = (err or "").split("\n")[0].strip()
    if not e:
        return "下载失败"
    if "全部下载失败" in e or "network_error" in e:
        return "站点无响应"
    if "timeout" in e.lower() or "timed out" in e.lower():
        return "站点响应超时"
    return e.split("（")[0][:40] or "下载失败"


def run_manga_batch(task_id: str, url: str):
    """漫画批量下载全话：每话页图放进 总文件夹/{话名}/ 二级子文件夹；
    某话失败跳过并继续，最后汇总成功数。
    每话有 4 分钟看门狗：单话卡死（DNS 失效/杀软锁文件等无超时原生阻塞）
    只损失这一话，整批照常往下走，不会再把任务永远挂在「下载中」。"""
    task = TASKS[task_id]
    try:
        name, chapters = _manga_chapters(url)
        if not chapters:
            raise RuntimeError("manga_parse 未找到任何章节链接")
        parent = title_out_dir(sanitize(f"{name}漫画" if name else "manga"),
                               task_id)
        total = len(chapters)
        fails, skipped = [], 0
        done_ok = 0
        for idx, ch in enumerate(chapters, 1):
            label = ch.get("label") or ""
            task.update({"status": "downloading",
                         "count": f"第 {idx}/{total} 话",
                         "episode": label,
                         "progress": int((idx - 1) * 100 / total)})
            # 先解析该话页图（顺便得到总页数），再做断点续传判断：
            # 「已下载」= PNG 张数 >= 总页数。旧逻辑只看「有没有 PNG」，
            # 中断后只转了一半的话会被误判成已下载，永远缺页。
            try:
                _n, chapter, images = _manga_parse_chapter(ch["url"])
            except Exception:
                fails.append(label or ch["url"])
                continue
            title = sanitize(_manga_file_title(name, chapter))
            ch_dir = os.path.join(parent, title)
            if _chapter_png_count(ch_dir) >= len(images):
                skipped += 1
                # 已齐全的话也要顺手收拾旧版本留下的 webp 残留，做到「只留 PNG」
                _chapter_png_only(ch_dir)
                task.update({"progress": int(idx * 100 / total)})
                continue
            # 单话看门狗：_pack_manga 放进工作线程限时执行（内部网络调用
            # 均有超时，但 DNS 解析/杀软锁文件等原生阻塞没有超时可言）
            done_box = {}

            def _chapter_worker(_t=title, _imgs=images, _i=idx, _u=ch["url"]):
                try:
                    done_box["got"] = _pack_manga(
                        task_id, _t, _imgs, _u, parent_dir=parent,
                        on_progress=lambda d, tt, i=_i: task.update(
                            {"progress": int((i - 1 + d / tt) * 100 / total)}))
                    done_box["ok"] = True
                except Exception as e:
                    done_box["ok"] = False
                    done_box["err"] = str(e)[:120]

            th = threading.Thread(target=_chapter_worker, daemon=True)
            th.start()
            th.join(240)
            if not done_box:
                fails.append((label or ch["url"]) + "（超时跳过）")
            elif done_box.get("ok") and (done_box.get("got") or 0) >= len(images):
                done_ok += 1
            elif done_box.get("ok"):
                # 有一页没落地（站点缺图/接口截断）也算没下全，如实报出来
                miss = len(images) - (done_box.get("got") or 0)
                fails.append(f"{label or ch['url']}（缺 {miss} 页）")
            else:
                fails.append(f"{label or ch['url']}（{_short_reason(done_box.get('err', ''))}）")
        # 结果文案说清楚：成功几话、跳过几话（本来就已下好）、失败几话
        count_txt = f"成功 {done_ok} 话 / 共 {total} 话"
        if skipped:
            count_txt += f"，跳过已下载 {skipped} 话"
        if fails:
            count_txt += f"，失败 {len(fails)} 话"
        task.update({"status": "done", "progress": 100,
                     "count": count_txt,
                     "ok_count": done_ok,
                     "skip_count": skipped,
                     "fail_count": len(fails),
                     "total_count": total,
                     "filepath": parent,
                     "filename": os.path.basename(parent),
                     "failed": fails})
    except Exception as e:
        code = classify_error(str(e))
        task.update({"status": "error",
                     "error": ZH_ERRORS.get(code, "下载失败"),
                     "detail": str(e)[:300],
                     "error_code": code})


def analyze_url(url: str, proxy: str = "", cookie_browser: str = "") -> dict:
    """解析链接，返回标题/作者/封面/时长/可用分辨率/图集/内容类型等元数据"""
    # 汽水音乐专用通道（yt-dlp 不认 qishui.douyin.com，先于通用流程处理）
    qs = _qishui_analyze(url)
    if qs:
        return qs
    # 影视聚合站专用通道（详情页无视频源，需先解析播放线路）
    film = _film_analyze(url)
    if film:
        return film
    info, used, last_err = None, "", None
    for label, base in parse_attempts(proxy, cookie_browser):
        opts = dict(base)
        opts["retries"] = 1
        opts["socket_timeout"] = 15
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            used = label
            break
        except Exception as e:
            last_err = e
            code = classify_error(str(e))
            # X（推特）的访客通道会限流，同一链接时好时坏：同方案自动重试两次
            if code == "X_LOGIN":
                for _ in range(2):
                    time.sleep(2)
                    try:
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            info = ydl.extract_info(url, download=False)
                        used = label
                        break
                    except Exception as e2:
                        last_err = e2
                if info is not None:
                    break
                continue
            # 平台不支持 / 网络不通：换 Cookie 也没用，直接报错
            if code in ("UNSUPPORTED", "PROXY", "XHS"):
                break
            # 只有「需要登录」才继续尝试浏览器登录态，其它错误不折腾
            if label == "" and code not in ("SIGNIN", "COOKIE_LOCK", "COOKIE_ENCRYPT"):
                break
            continue
    if info is None:
        raise last_err or RuntimeError("解析失败")



    heights = set()
    for f in info.get("formats") or []:
        if f.get("height"):
            heights.add(int(f["height"]))
    # 视频本身的高度也算上
    if info.get("height"):
        heights.add(int(info["height"]))

    tiers = []
    for threshold, label in RES_TIERS:
        if any(h >= threshold for h in heights):
            tiers.append(label)
    if not tiers and heights:
        best = max(heights)
        tiers = [height_label(best)]
    tiers = tiers[::-1]  # 从低到高排列

    duration = info.get("duration") or 0
    if duration:
        m, s = divmod(int(duration), 60)
        h, m = divmod(m, 60)
        duration_text = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    else:
        duration_text = "--:--"

    # 取最大尺寸的封面：thumbnails 里按分辨率挑最大，而不是直接用 thumbnail
    # （很多平台 thumbnail 只是 640x360 级别的默认小图，如 YouTube 的 hqdefault）
    cover = _best_cover(info)
    # 附带全部候选封面（大->小），前端保存封面时会逐个实测像素、挑最大的保存
    _cover_scored = sorted(
        [t for t in (info.get("thumbnails") or [])],
        key=lambda t: (t.get("width") or 0) * (t.get("height") or 0)
        or (t.get("preference") or 0), reverse=True)
    covers = _dedup_urls([_original_image_url(t.get("url") or "")
                          for t in _cover_scored if t.get("url")]
                         + ([_original_image_url(info["thumbnail"])]
                            if info.get("thumbnail") else []))[:6]

    images = _collect_images(info)
    has_video, has_audio = _has_video(info), _has_audio(info)

    # 类型判定：有视频流即视频 > 有图片即图集 > 只有音频即音乐；都没有则直接判失败（不出空壳卡）
    if has_video:
        content_type = "video"
    elif images:
        content_type = "image"
    elif has_audio:
        content_type = "audio"
    else:
        raise RuntimeError("no_media 该链接没有可提取的视频、图集或音频")

    # 视频/音频结果附带在线预览地址（点开即可播，无需先下载）
    preview_url, preview_silent, preview_audio = ("", False, "")
    if content_type in ("video", "audio"):
        preview_url, preview_silent, preview_audio = _pick_preview(info, content_type)

    # 可用性预检：视频/音频/图片的实际素材拉不到时，直接报失败，不展示空壳结果卡
    page = info.get("webpage_url") or url
    if content_type == "image":
        probe = [i.get("url") for i in images[:1]]
    else:
        probe = [preview_url] or []
        if not any(probe):
            # 只有 m3u8/DASH 分片流时没有预览直链，退而探测首个格式地址
            probe = [f.get("url") for f in (info.get("formats") or []) if f.get("url")][:1]
    if any(probe) and not _any_reachable(probe, page, proxy, cookie_browser):
        if proxy:
            raise RuntimeError(
                "network_error 素材无法访问：该平台的视频/图片服务器在当前网络下打不开，"
                "多为代理节点不通或被限制，请更换代理节点后重试")
        raise RuntimeError(
            "proxy_needed 素材无法直连：该平台需要代理才能访问，当前没有填写代理地址")

    # 音乐结果附带歌词（LRC 原文 + 翻译，当前支持网易云，其他平台留空）
    lyrics, lyrics_trans = ("", "")
    if content_type == "audio":
        lyrics, lyrics_trans = _netease_lyrics(info.get("webpage_url") or url)

    return {
        "title": info.get("title") or "未知标题",
        "author": (info.get("uploader") or info.get("channel") or info.get("artist")
                   or info.get("creator") or info.get("uploader_id") or "未知作者"),
        "lyrics": lyrics,
        "lyrics_trans": lyrics_trans,
        "cover": cover,
        "covers": covers,
        "duration": duration_text,
        "platform": (info.get("extractor_key") or "unknown").split(":")[0],
        "webpage": info.get("webpage_url") or url,
        "resolutions": tiers,
        "views": info.get("view_count"),
        "is_live": bool(info.get("is_live")),
        "content_type": content_type,
        "image_count": len(images),
        # 前端只预览前 24 张，避免返回过大
        "images": images[:24],
        "ext": (info.get("ext") or "").lower(),
        "preview_url": preview_url,
        "preview_silent": preview_silent,
        "preview_audio_url": preview_audio,
        "cookie_used": used,
    }



def _probe_url(u: str, webpage: str, ydl, range_hint: bool = True):
    """用很小的请求探测某个媒体地址能否拉到（Range 被拒时再试完整请求）"""
    if not u:
        return False
    attempts = [{"Range": "bytes=0-4095"}, {}] if range_hint else [{}]
    for extra in attempts:
        try:
            headers = {"User-Agent": UA}
            headers.update(extra)
            if webpage:
                headers["Referer"] = webpage
            resp = ydl.urlopen(sanitized_Request(u, headers=headers))
            resp.read(64)
            return True
        except Exception:
            continue
    return False


def _any_reachable(urls, webpage: str, proxy: str = "", cookie_browser: str = ""):
    """
    结果可用性预检：视频流 / 音频流 / 图集图片只要有一张能拉到就算可用。
    全都拉不到说明该素材所在的 CDN 在当前网络下打不开（典型：没挂代理访问 X），
    此时结果卡只是个空壳（封面、缩略图、预览、下载全废），不如直接报失败原因。
    """
    urls = [u for u in (urls or []) if u]
    if not urls:
        return True
    opts = base_opts(proxy, cookie_browser)
    opts["retries"] = 0
    opts["socket_timeout"] = 5
    ydl = yt_dlp.YoutubeDL(opts)
    for u in urls[:1]:
        if _probe_url(u, webpage, ydl, range_hint=False):
            return True
    return False


def _fetch_image(ydl, url: str, path: str, referer: str = ""):
    """用 yt-dlp 的 http 栈下载单张图片（自动带上代理与 Cookie）"""
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    resp = ydl.urlopen(sanitized_Request(url, headers=headers))
    status = getattr(resp, "status", 200)
    if status and int(status) >= 400:
        raise RuntimeError(f"HTTP {status}")
    size = 0
    with open(path, "wb") as fp:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            fp.write(chunk)
            size += len(chunk)
    if size < 512:
        raise RuntimeError("图片内容为空")
    return size


def _images_from_page(url: str, opts: dict):
    """兜底：直接抓页面里的 og:image / twitter:image（部分图集站不在 yt-dlp 支持列表里）"""
    try:
        ydl = yt_dlp.YoutubeDL(opts)
        resp = ydl.urlopen(sanitized_Request(url, headers={"User-Agent": UA}))
        html = resp.read(3 << 20).decode("utf-8", errors="ignore")
    except Exception:
        return []
    found, seen = [], set()
    for pat in (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)'):
        for m in re.finditer(pat, html, re.I):
            u = m.group(1)
            if u.startswith("//"):
                u = "https:" + u
            if u.startswith("http") and u not in seen:
                seen.add(u)
                ext = os.path.splitext(u.split("?")[0])[1].lstrip(".").lower()
                found.append({"url": _original_image_url(u), "src": u,
                              "ext": ext if ext in IMG_EXTS else "jpg"})
    return found


def _is_image_bytes(head: bytes) -> bool:
    """校验响应开头是不是常见图片魔数（防 CDN 错误页/截断响应当图片保存）"""
    return bool(head and (
        head[:4] == b"RIFF" and head[8:12] == b"WEBP"
        or head.startswith(b"\x89PNG")
        or head.startswith(b"\xff\xd8")
        or head.startswith(b"GIF8")
        or head.startswith(b"BM")))


_UPSCALE_MIN_SIDE = 960    # 短边低于该值视为小图：下载/保存前自动放大


def _upscale_if_small(im):
    """「图片放大」步骤：短边不足阈值的小图用 LANCZOS 放大（上限 4 倍），
    再轻度锐化补回边缘感。达到阈值的不动（避免无损原图被无谓重采样）"""
    from PIL import Image, ImageFilter
    w, h = im.size
    short = min(w, h)
    if short <= 0 or short >= _UPSCALE_MIN_SIDE:
        return im
    factor = min(4.0, _UPSCALE_MIN_SIDE / short)
    im = im.resize((max(1, round(w * factor)), max(1, round(h * factor))),
                   Image.LANCZOS)
    try:
        im = im.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=2))
    except Exception:
        pass
    return im


def _upscale_small_file(path: str) -> str:
    """封面等单图的「图片放大」：短边不足时按原格式重写为放大版（保持扩展名不变）"""
    fmt_map = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP",
               "bmp": "BMP", "gif": "GIF"}
    fmt = fmt_map.get(os.path.splitext(path)[1].lstrip(".").lower())
    if not fmt:
        return path
    tmp = path + ".up"
    try:
        from PIL import Image
        with Image.open(path) as im:
            if getattr(im, "n_frames", 1) > 1:      # 动图不放大
                return path
            up = _upscale_if_small(im)
            if up is im:
                return path
            params = {"quality": 92} if fmt == "JPEG" else \
                {"quality": 90} if fmt == "WEBP" else {}
            up.save(tmp, fmt, **params)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return path


def _upscale_png_inplace(src: str) -> str:
    """已是 PNG 的图：仅当短边不足阈值时放大重写（原路径覆盖，其余原样返回）"""
    tmp = src + ".up"
    try:
        from PIL import Image
        with Image.open(src) as im:
            if getattr(im, "n_frames", 1) > 1:
                return src
            up = _upscale_if_small(im)
            if up is im:
                return src
            up.save(tmp, "PNG", compress_level=3)
        os.replace(tmp, src)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return src


def _convert_image_png(src: str) -> str:
    """图片统一转 PNG（无损重编码，像素不变、清晰度不降），小图先放大。
    已是 png 直接走原格式放大检测；动图（gif/动态 webp）转 PNG 会丢帧，保留原格式；
    转不出来（文件损坏等）抛错，由调用方把这一页算作失败，不留垃圾文件。"""
    if src.lower().endswith(".png"):
        return _upscale_png_inplace(src)
    dst = os.path.splitext(src)[0] + ".png"
    try:
        from PIL import Image
        with Image.open(src) as im:
            if getattr(im, "n_frames", 1) > 1:
                return src
            im.load()
            im = _upscale_if_small(im)          # 「图片放大」：小图先放大再落盘
            im.save(dst, "PNG", compress_level=3)   # 3 档：体积同 6 档、编码快 ~30%
        # 删除原图只是清理，绝不是关键路径：删不掉就保留双文件，PNG 成果必须保住
        try:
            os.remove(src)
        except OSError:
            pass
        return dst
    except ImportError:
        pass
    except Exception:
        pass
    try:
        subprocess.run([FFMPEG, "-y", "-i", src, dst],
                       capture_output=True, timeout=30)
        if os.path.exists(dst) and os.path.getsize(dst) > 1024:
            try:
                os.remove(src)
            except OSError:
                pass
            return dst
    except Exception:
        pass
    raise RuntimeError(f"图片损坏，无法转 PNG: {os.path.basename(src)}")


def _pack_images(task_id: str, title: str, images, webpage: str,
                 proxy: str = "", cookie_browser: str = ""):
    """把图片列表逐张下载并统一导出为高清 PNG（散图存放，不打包 ZIP）"""
    task = TASKS[task_id]
    out_dir = title_out_dir(title, task_id)
    os.makedirs(out_dir, exist_ok=True)

    opts = base_opts(proxy, cookie_browser)
    total = len(images)
    task.update({"status": "downloading", "progress": 0, "total": total, "count": f"0/{total}"})

    ydl = yt_dlp.YoutubeDL(opts)
    saved, errs = [], []
    for i, img in enumerate(images, 1):
        ext = img["ext"] if img["ext"] in IMG_EXTS else "jpg"
        path = os.path.join(out_dir, f"{title}_{i:03d}.{ext}")
        best = _original_image_url(img.get("url") or "")
        raw = img.get("src") or img.get("url") or best
        ok = False
        for u in (best, raw):
            if ok or not u:
                continue
            try:
                _fetch_image(ydl, u, path, referer=webpage)
                ok = True
            except Exception as e:
                errs.append(str(e))
        if ok:
            saved.append(path)
        task.update({
            "status": "downloading",
            "progress": int(i * 100 / total),
            "count": f"{i}/{total}",
        })

    if not saved:
        detail = "；".join(errs[-3:])[:200]
        low = detail.lower()
        if any(k in low for k in ("timed out", "timeout", "connection", "tunnel",
                                  "proxy", "getaddrinfo", "unreachable", "403",
                                  "429", "502", "503", "network_error")):
            raise RuntimeError("network_error 图片被网络限制，无法下载（" + detail + "）")
        raise RuntimeError("图片下载失败：可能需要登录，或该平台禁止外链访问图片（"
                           + detail + "）")

    # 统一转高清 PNG（无损重编码；个别动图/转换失败保留原格式）
    task.update({"status": "processing", "progress": 97, "count": f"{len(saved)}/{total}"})
    ok_saved = []
    n_conv = len(saved)
    for j, p in enumerate(saved):
        try:
            ok_saved.append(_convert_image_png(p))
        except Exception:
            try:
                os.remove(p)      # 损坏页丢弃，不留垃圾文件
            except OSError:
                pass
        # 转换阶段也要动进度条（97 -> 99），否则大图集会长时间停在 97 像卡死
        if n_conv:
            task.update({"status": "processing",
                         "progress": 97 + int((j + 1) * 2 / n_conv),
                         "count": f"{j + 1}/{total}"})
    saved = ok_saved

    # 散图直接存放（不再打包 ZIP），文件管理器里定位到所在文件夹
    filepath = saved[0] if len(saved) == 1 else out_dir

    task.update({
        "status": "done",
        "progress": 100,
        "count": f"{len(saved)}/{total}",
        "filepath": filepath,
        "filename": os.path.basename(filepath),
    })


def run_image_download(task_id: str, url: str, proxy: str = "", cookie_browser: str = ""):
    """图集下载：解析出所有图片地址 -> 逐张下载 -> 多张打包为 ZIP"""
    task = TASKS[task_id]

    info, used, last_err, opts = None, "", None, {}
    for label, base in parse_attempts(proxy, cookie_browser):
        o = dict(base)
        o["retries"] = 1
        o["socket_timeout"] = 20
        try:
            with yt_dlp.YoutubeDL(o) as ydl:
                info = ydl.extract_info(url, download=False)
            used, opts = label, o
            break
        except Exception as e:
            last_err = e
            code = classify_error(str(e))
            if code in ("UNSUPPORTED", "PROXY", "XHS"):
                break
            if label == "" and code not in ("SIGNIN", "COOKIE_LOCK", "COOKIE_ENCRYPT"):
                break
            continue
    if info is None:
        # X/推特纯图片帖：yt-dlp 误报「No video could be found」，走图集专用通道
        if TW_URL_RE.match(url):
            tw = _twitter_fetch(url, proxy, cookie_browser)
            if tw:
                _pack_images(task_id, sanitize(tw["title"]), tw["images"],
                             tw["webpage"], proxy, cookie_browser)
                return
        raise last_err or RuntimeError("解析失败")

    images = _collect_images(info)
    webpage = info.get("webpage_url") or url
    if not images:
        images = _images_from_page(webpage, opts)
    if not images:
        raise RuntimeError("未在该链接中找到图片（该内容可能是视频，或平台限制了图片地址）")

    title = sanitize(info.get("title") or "images")
    _pack_images(task_id, title, images, webpage, proxy, cookie_browser)


def _qishui_download(task_id: str, url: str):
    """汽水音乐下载：直下平台音频流 -> 统一转 MP3 -> 嵌歌词 -> 按标题归档"""
    task = TASKS[task_id]
    out_dir = os.path.join(DOWNLOAD_DIR, task_id)
    os.makedirs(out_dir, exist_ok=True)
    aw = _qishui_page_data(url)
    if not aw:
        raise RuntimeError("qishui_parse 汽水音乐页面数据获取失败，请重新分享链接后重试")
    audio_url = (aw.get("url") or "").strip()
    if not audio_url:
        hint = ((aw.get("trackInfo") or {}).get("display_hint") or {}).get("view_only_msg") or ""
        raise RuntimeError("qishui_no_audio 平台未提供该歌曲的音频源，任何工具都无法下载："
                           + (hint or "该音源因版权调整暂不可用，或下载需要平台会员"))
    title = sanitize(aw.get("trackName") or "qishui")
    task.update({"status": "downloading", "progress": 3})
    req = urllib.request.Request(audio_url, headers={
        "User-Agent": UA, "Referer": "https://music.douyin.com/"})
    tmp = os.path.join(out_dir, title + ".src")
    done, total = 0, 0
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as fp:
        total = int(r.headers.get("Content-Length") or 0)
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            fp.write(chunk)
            done += len(chunk)
            task.update({"status": "downloading",
                         "progress": min(90, int(done * 90 / total)) if total else 50,
                         "speed": ""})
    # 统一转 MP3（兼容性最好）
    task.update({"status": "processing", "progress": 92})
    mp3 = os.path.join(out_dir, title + ".mp3")
    try:
        subprocess.run([FFMPEG, "-y", "-i", tmp, "-vn", "-b:a", "192k", mp3],
                       capture_output=True, timeout=300, check=True)
    except Exception:
        shutil.move(tmp, mp3)            # 转换失败退回原始文件，保证有产物
    if os.path.abspath(tmp) != os.path.abspath(mp3) and os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    # 内嵌歌词（汽水自带逐句时间戳，直接写 USLT）
    lrc = _qishui_lrc(aw.get("lyrics"))
    if lrc and mp3.lower().endswith(".mp3"):
        _embed_lyrics(mp3, lrc, "")
    # 归档：按标题归入同一链接的文件夹
    try:
        dest_dir = title_out_dir(title, task_id)
        dest = os.path.join(dest_dir, os.path.basename(mp3))
        if os.path.abspath(dest) != os.path.abspath(mp3):
            if os.path.exists(dest):
                try:
                    os.remove(dest)
                except OSError:
                    pass
            shutil.move(mp3, dest)
            mp3 = dest
            try:
                if not os.listdir(out_dir):
                    os.rmdir(out_dir)
            except OSError:
                pass
    except OSError:
        pass
    task.update({"status": "done", "progress": 100,
                 "filepath": mp3, "filename": os.path.basename(mp3)})


def run_download(task_id: str, url: str, kind: str, height: int,
                 proxy: str = "", cookie_browser: str = "", play_url: str = ""):
    """在后台线程执行下载，通过 progress_hooks 上报进度"""
    task = TASKS[task_id]
    out_dir = os.path.join(DOWNLOAD_DIR, task_id)
    os.makedirs(out_dir, exist_ok=True)

    if kind == "images":
        try:
            run_image_download(task_id, url, proxy, cookie_browser)
        except Exception as e:
            code = classify_error(str(e))
            task.update({"status": "error",
                         "error": ZH_ERRORS.get(code, "下载失败"),
                         "detail": str(e)[:300],
                         "error_code": code})
        return

    if _QISHUI_URL_RE.search(url or ""):
        # 汽水音乐：yt-dlp 不认该站点，走专用下载（直下音频流 -> 转 MP3 -> 嵌歌词）
        try:
            _qishui_download(task_id, url)
        except Exception as e:
            code = classify_error(str(e))
            task.update({"status": "error",
                         "error": ZH_ERRORS.get(code, "下载失败"),
                         "detail": str(e)[:300],
                         "error_code": code})
        return

    if _film_site(url):
        # 影视站：详情页里没有视频源，先解析出 m3u8 再交给 yt-dlp（HLS 自动合成 MP4）
        try:
            _film_download(task_id, url, play_url=play_url)
        except Exception as e:
            code = classify_error(str(e))
            task.update({"status": "error",
                         "error": ZH_ERRORS.get(code, "下载失败"),
                         "detail": str(e)[:300],
                         "error_code": code})
        return

    film_err = None
    if _film_looks_like(url):
        # 未收录的同类影视站：泛化尝试，失败则回落通用 yt-dlp 下载
        try:
            _film_download(task_id, url, play_url=play_url)
            return
        except Exception as e:
            film_err = str(e)

    if kind == "audio":
        # 提取音频：统一转 MP3，兼容性最好
        fmt = "bestaudio/best"
        postprocessors = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
        merge_fmt = None
    elif kind == "music":
        # 下载音乐：统一直接转成通用 MP3（兼容性最好，避免 m4a/NCM 等格式放不出来）
        fmt = "bestaudio/best"
        postprocessors = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "0",
        }]
        merge_fmt = None
    else:
        fmt = _fmt_selector(height)
        postprocessors = []
        merge_fmt = "mp4"


    opts_common = {
        "format": fmt,
        "outtmpl": os.path.join(out_dir, "%(title).100s.%(ext)s"),
        "progress_hooks": [lambda d: progress_hook(task_id, d)],
        "postprocessor_hooks": [lambda d: pp_hook(task_id, d)],
    }
    if merge_fmt:
        opts_common["merge_output_format"] = merge_fmt
    if postprocessors:
        opts_common["postprocessors"] = postprocessors

    try:
        last_err = None
        downloaded = False
        for label, base in parse_attempts(proxy, cookie_browser):
            opts = dict(base)
            opts.update(opts_common)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
                downloaded = True
                break
            except Exception as e:
                last_err = e
                # 只有「需要登录」类失败才换浏览器登录态重试
                if label == "" and _needs_login(str(e)):
                    for f in os.listdir(out_dir):
                        try:
                            os.remove(os.path.join(out_dir, f))
                        except OSError:
                            pass
                    continue
                raise
        if not downloaded and last_err:
            raise last_err
        # 找到产出文件
        files = [os.path.join(out_dir, f) for f in os.listdir(out_dir)]
        files = [f for f in files if not f.endswith(".part")]
        if not files:
            raise RuntimeError("未找到下载产物")
        filepath = max(files, key=os.path.getsize)
        # 归档：按标题归入同一链接的文件夹（yt-dlp 的文件名就是标题）
        try:
            stem = os.path.splitext(os.path.basename(filepath))[0]
            dest_dir = title_out_dir(stem, task_id)
            dest = os.path.join(dest_dir, os.path.basename(filepath))
            if os.path.abspath(dest) != os.path.abspath(filepath):
                if os.path.exists(dest):        # 同一链接重复下载 -> 覆盖旧文件
                    try:
                        os.remove(dest)
                    except OSError:
                        pass
                shutil.move(filepath, dest)
                filepath = dest
                try:                             # 任务暂存目录空了就顺手删掉
                    if not os.listdir(out_dir):
                        os.rmdir(out_dir)
                except OSError:
                    pass
        except OSError:
            pass                                 # 移不动就留在任务目录，不影响交付
        # 音乐/音频下载：把歌词写进 MP3 的 USLT 帧（带时间戳，播放器可直接滚动显示）
        if kind in ("music", "audio") and filepath.lower().endswith(".mp3"):
            lrc, tly = _netease_lyrics(url)
            if lrc:
                _embed_lyrics(filepath, lrc, tly)
        # 微博视频：平台把「微博视频号 @作者」角标烧进了转码文件，
        # 检测到角标就本地修补掉（delogo 邻域插值）；失败/探测不到保留原文件
        if _is_weibo(url) and filepath.lower().endswith(".mp4"):
            filepath = _weibo_debadge(task_id, filepath)
        task.update({
            "status": "done",
            "progress": 100,
            "filepath": filepath,
            "filename": os.path.basename(filepath),
        })
    except Exception as e:
        # X 视频推文：yt-dlp 对未登录访客常报 unavailable/要求登录，
        # 这里改走镜像直链下载（与解析侧同一兜底），不把「要求登录」甩给用户
        if TW_URL_RE.match(url) and _twitter_direct_download(
                task_id, url, height, proxy, cookie_browser):
            return
        code = classify_error(str(e))
        detail = str(e)[:300]
        if film_err and code in ("UNSUPPORTED", "GENERIC"):
            # 同类影视站泛化解析失败过，且通用下载也认不出：优先给影视站的明确原因
            code = classify_error(film_err)
            detail = film_err[:300]
        task.update({"status": "error",
                     "error": ZH_ERRORS.get(code, "下载失败"),
                     "detail": detail,
                     "error_code": code})


def _is_weibo(url: str) -> bool:
    u = (url or "").lower()
    return "weibo.com" in u or "weibo.cn" in u or "weibocdn.com" in u


def _ffmpeg_duration(path: str) -> float:
    try:
        r = subprocess.run([FFMPEG, "-i", path], capture_output=True, timeout=30)
        m = re.search(rb"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or b"")
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return 0.0


def _weibo_badge_box(path: str):
    """探测微博视频右上角的「微博视频号 @作者」半透明角标（平台转码时烧进画面）。
    抽多帧做像素分析：角标是固定叠加在右上角的高亮文字带（logo 行 + @昵称行）。
    舞台灯光/白墙等亮背景会产生干扰，但角标跨帧位置不变、干扰每次都不同——
    所以按「跨帧复现」取候选（帧太少时退一步要求两行文字带签名）。
    探测可信返回 ffmpeg delogo 参数 (x, y, w, h)，否则返回 None
    （宁可不处理，也绝不在没角标的视频上乱抹）"""
    from PIL import Image
    cands = []
    for ss in ("1", "3", "6"):
        frame = f"{path}.wb{ss}.png"
        try:
            r = subprocess.run([FFMPEG, "-y", "-ss", ss, "-i", path,
                                "-frames:v", "1", frame],
                               capture_output=True, timeout=60)
            if r.returncode != 0 or not os.path.exists(frame) \
                    or os.path.getsize(frame) < 1024:
                continue
            im = Image.open(frame).convert("L")
            W, H = im.size
            if W < 320 or H < 240:
                continue
            px = im.load()
            x0, x1 = int(W * 0.40), W - 1
            y0, y1 = int(H * 0.01), int(H * 0.15)
            step = 2 if (x1 - x0) > 300 else 1
            nsamp = max(1, (x1 - x0 + step - 1) // step)
            rows = []
            for y in range(y0, y1):
                cnt = 0
                for x in range(x0, x1, step):
                    if px[x, y] > 170:
                        cnt += 1
                rows.append((y, cnt))
            # 背景大面积发白（雪地/白墙/强灯光满行）时该帧不可信
            if any(cnt > nsamp * 0.6 for _, cnt in rows):
                continue
            band = [y for y, cnt in rows if cnt >= max(4, int(nsamp * 0.06))]
            if not band:
                continue
            by0, by1 = min(band), max(band)
            if by1 - by0 > H * 0.10 or by0 > H * 0.12:
                continue
            # 两行文字带签名：行簇之间有间断（logo 行 + @昵称行）
            gaps, prev = 0, None
            clusters = 0
            for yy in band:
                if prev is None or yy - prev > 2:
                    clusters += 1
                prev = yy
            two_row = clusters >= 2
            cols = []
            for x in range(x0, x1, step):
                cnt = sum(1 for y in range(by0, by1 + 1) if px[x, y] > 170)
                if cnt >= max(2, (by1 - by0) // 6):
                    cols.append(x)
            if not cols:
                continue
            bx0, bx1 = min(cols), max(cols)
            # 角标贴右上角：右缘距画面右边不超过 8% 宽度
            if W - bx1 > W * 0.08 or bx1 - bx0 < W * 0.06:
                continue
            cands.append((bx0, by0, bx1, by1, W, H, two_row))
        except Exception:
            continue
        finally:
            try:
                if os.path.exists(frame):
                    os.remove(frame)
            except OSError:
                pass
    if not cands:
        return None

    def _overlap(a, b):
        ax0, ay0, ax1, ay1 = a[:4]
        bx0, by0, bx1, by1 = b[:4]
        ix = max(0, min(ax1, bx1) - max(ax0, bx0))
        iy = max(0, min(ay1, by1) - max(ay0, by0))
        inter = ix * iy
        union = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
        return inter / union if union else 0.0

    # 跨帧复现的候选（角标静止、干扰帧帧不同）
    group = [c for c in cands
             if any(d is not c and _overlap(c, d) >= 0.5 for d in cands)]
    if not group:
        # 无跨帧复现：仅接受签名极强的单帧候选（两行文字带 + 位置贴角）
        strong = [c for c in cands if c[6]]
        if len(strong) != 1:
            return None
        group = strong
    xs0 = min(b[0] for b in group)
    ys0 = min(b[1] for b in group)
    xs1 = max(b[2] for b in group)
    ys1 = max(b[3] for b in group)
    W, H = group[0][4], group[0][5]
    pad = max(4, W // 150)
    x = max(1, xs0 - pad)
    y = max(1, ys0 - pad)
    w = min(xs1 + pad, W - 2) - x
    h = min(ys1 + pad, H - 2) - y
    if w < 20 or h < 10:
        return None
    return x, y, w, h


def _weibo_debadge(task_id: str, path: str) -> str:
    """微博视频去角标：只有探测到角标才重编码修补；任何失败都保留原文件"""
    task = TASKS[task_id]
    try:
        box = _weibo_badge_box(path)
    except Exception:
        box = None
    if not box:
        return path
    x, y, w, h = box
    out = path + ".debadge.mp4"
    dur = _ffmpeg_duration(path)
    task.update({"status": "processing", "progress": 90,
                 "detail": "weibo_debadge"})
    try:
        proc = subprocess.Popen(
            [FFMPEG, "-y", "-i", path,
             "-vf", f"delogo=x={x}:y={y}:w={w}:h={h}",
             "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
             "-c:a", "copy", "-movflags", "+faststart",
             "-progress", "pipe:1", "-nostats", out],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="ignore")
        for line in (proc.stdout or ()):
            if line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    us = int(line.split("=", 1)[1].strip())
                except ValueError:
                    continue
                if dur and us > 0:
                    task.update({"status": "processing",
                                 "progress": min(99, 90 + int(us * 9 / (dur * 1e6)))})
        proc.wait(timeout=3600)
        if proc.returncode == 0 and os.path.exists(out) \
                and os.path.getsize(out) > max(4096, os.path.getsize(path) // 20):
            try:
                os.replace(out, path)
            except OSError:
                try:
                    os.remove(path)
                except OSError:
                    pass
                shutil.move(out, path)
            return path
    except Exception:
        pass
    try:
        if os.path.exists(out):
            os.remove(out)
    except OSError:
        pass
    return path


def progress_hook(task_id: str, d: dict):
    task = TASKS[task_id]
    if d["status"] == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes") or 0
        pct = int(done * 100 / total) if total else 0
        speed = d.get("speed")
        eta = d.get("eta")
        task.update({
            "status": "downloading",
            "progress": pct,
            "speed": f"{speed / 1048576:.1f} MB/s" if speed else "",
            "eta": int(eta) if eta else 0,
        })
    elif d["status"] == "finished":
        task.update({"status": "processing", "progress": 95})


def pp_hook(task_id: str, d: dict):
    if d.get("status") == "finished":
        TASKS[task_id].update({"status": "processing", "progress": 97})


# ---------------- X/Twitter 图集专用通道（yt-dlp 不认纯图片帖） ----------------
TW_URL_RE = re.compile(r"^https?://(www\.|mobile\.)?(twitter\.com|x\.com)/", re.I)
TW_STATUS_RE = re.compile(r"/(?:twitter|x)\.com/[^/]+/status(?:es)?/(\d+)", re.I)


def _twitter_fetch(url: str, proxy: str = "", cookie_browser: str = ""):
    """
    推文数据兜底：yt-dlp 的 Twitter 提取器只支持视频，纯图片帖一律误报
    「No video could be found in this tweet」。这里通过公开镜像 API 取整份推文，
    拿到全部原图（name=orig 无压缩）。成功返回 dict(title/author/views/webpage/images)，
    失败返回 None
    """
    m = TW_STATUS_RE.search(url or "")
    if not m:
        return None
    data = None
    last = None
    # 先走用户填的代理；失败后再试一次直连（镜像接口很多网络下可以直连）
    for p in ((proxy, "") if proxy else ("",)):
        opts = base_opts(p, cookie_browser)
        opts["retries"] = 1
        opts["socket_timeout"] = 15
        try:
            ydl = yt_dlp.YoutubeDL(opts)
            resp = ydl.urlopen(sanitized_Request(
                f"https://api.fxtwitter.com/status/{m.group(1)}",
                headers={"User-Agent": UA, "Accept": "application/json"}))
            data = json.loads(resp.read(4 << 20).decode("utf-8", errors="ignore"))
            break
        except Exception as e:
            data = None
            last = e
    if data is None:
        # 两条路都不通，说明网络/节点受限，要如实告诉用户而不是误报「没有内容」
        low = str(last).lower()
        if any(k in low for k in ("timed out", "timeout", "connection", "tunnel",
                                  "proxy", "getaddrinfo", "unreachable", "name or service",
                                  "503", "502", "ssl", "certificate")):
            raise RuntimeError("network_error 无法访问 X 的数据接口（api.fxtwitter.com）："
                               + str(last)[:160])
        return None
    tweet = data.get("tweet") or {}
    media = tweet.get("media") or {}
    photos = media.get("photos") or []
    # 带视频的推文 yt-dlp 本身就能解析，这里只兜底纯图集
    if not photos or media.get("videos"):
        return None
    images = []
    for p in photos:
        u = p.get("url") or ""
        if not u:
            continue
        images.append({"url": _original_image_url(u), "src": u,
                       "ext": "png" if ".png" in u.lower() else "jpg"})
    if not images:
        return None
    author = tweet.get("author") or {}
    return {
        "title": (tweet.get("text") or "").strip() or "X/Twitter 图集",
        "author": author.get("name") or author.get("screen_name") or "X 用户",
        "views": tweet.get("views"),
        "webpage": tweet.get("url") or url,
        "images": images,
    }


def _twitter_gallery_response(tw, proxy: str = "", cookie_browser: str = ""):
    """把推特图集数据转成前端结果；图床拉不到时直接返回失败原因，不出空壳结果卡"""
    imgs = tw["images"]
    if not _any_reachable([i.get("url") for i in imgs[:1]],
                          tw["webpage"], proxy, cookie_browser):
        code = "NETWORK" if proxy else "PROXY_NEEDED"
        return jsonify({"error": ZH_ERRORS[code], "code": code,
                        "detail": "image CDN unreachable: " + imgs[0]["url"][:120]}), 400
    return jsonify({
        "title": tw["title"], "author": tw["author"],
        "cover": imgs[0]["url"], "duration": "--:--",
        "platform": "Twitter", "webpage": tw["webpage"],
        "resolutions": [], "views": tw["views"], "is_live": False,
        "content_type": "image", "image_count": len(imgs),
        "images": imgs[:24], "ext": imgs[0]["ext"],
        "preview_url": "", "preview_silent": False,
        "preview_audio_url": "",
    })


def _twitter_video_fetch(url: str, proxy: str = "", cookie_browser: str = ""):
    """X 视频推文兜底：yt-dlp 对未登录访客常报「Video N is unavailable」，
    用 fxtwitter 镜像直接拿视频直链（无需登录）。
    成功返回 dict(title/author/webpage/cover/videos[{url,height,width}])，失败返回 None"""
    m = TW_STATUS_RE.search(url or "")
    if not m:
        return None
    data = None
    # 先走用户填的代理；失败后再试一次直连
    for p in ((proxy, "") if proxy else ("",)):
        opts = base_opts(p, cookie_browser)
        opts["retries"] = 1
        opts["socket_timeout"] = 15
        try:
            ydl = yt_dlp.YoutubeDL(opts)
            resp = ydl.urlopen(sanitized_Request(
                f"https://api.fxtwitter.com/status/{m.group(1)}",
                headers={"User-Agent": UA, "Accept": "application/json"}))
            data = json.loads(resp.read(4 << 20).decode("utf-8", errors="ignore"))
            break
        except Exception:
            data = None
    if not data:
        return None
    tweet = data.get("tweet") or {}
    media = tweet.get("media") or {}
    vids = []
    for v in media.get("videos") or []:
        u = v.get("url") or ""
        if not u:
            continue
        try:
            h = int(v.get("height") or 0)
            w = int(v.get("width") or 0)
        except (TypeError, ValueError):
            h = w = 0
        vids.append({"url": u, "height": h, "width": w})
    if not vids:
        return None
    author = tweet.get("author") or {}
    return {
        "title": (tweet.get("text") or "").strip() or "X/Twitter 视频",
        "author": author.get("name") or author.get("screen_name") or "X 用户",
        "webpage": tweet.get("url") or url,
        "cover": ((media.get("thumbnail") or {}).get("url")) or "",
        "videos": vids,
    }


def _twitter_video_response(tv):
    """把镜像视频数据转成前端视频结果卡（ resolutions 低->高，最高一档标「原画」）"""
    vids = sorted(tv["videos"], key=lambda v: v["height"] or 0)
    top = vids[-1]
    labels = []
    for v in vids:
        h = v["height"]
        labels.append("原画" if v is top and h else
                      (f"{h}P" if h else "默认"))
    return jsonify({
        "title": tv["title"], "author": tv["author"],
        "cover": tv["cover"], "duration": "--:--",
        "platform": "Twitter", "webpage": tv["webpage"],
        "resolutions": labels, "views": None, "is_live": False,
        "content_type": "video", "ext": "mp4",
        "preview_url": top["url"], "preview_silent": False,
        "preview_audio_url": "",
    })


def _twitter_direct_download(task_id: str, url: str, height: int = 0,
                             proxy: str = "", cookie_browser: str = "") -> bool:
    """X 视频下载兜底：yt-dlp 失败后用镜像直链直接下载。
    拿到视频并完成下载返回 True；拿不到返回 False（调用方回落原错误）"""
    task = TASKS[task_id]
    try:
        tv = _twitter_video_fetch(url, proxy, cookie_browser)
    except Exception:
        tv = None
    if not tv:
        return False
    vids = sorted(tv["videos"], key=lambda v: v["height"] or 0)
    if height:
        cands = [v for v in vids if v["height"] and v["height"] <= height]
        pick = cands[-1] if cands else vids[0]
    else:
        pick = vids[-1]
    title = sanitize(tv["title"])
    out_dir = os.path.join(DOWNLOAD_DIR, task_id)
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, title + ".mp4")
    task.update({"status": "downloading", "progress": 3})
    headers = {"User-Agent": UA, "Referer": "https://x.com/"}
    opts = base_opts(proxy, cookie_browser)
    done, total = 0, 0
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            resp = ydl.urlopen(sanitized_Request(pick["url"], headers=headers))
        with resp:
            try:
                total = int(resp.headers.get("Content-Length") or 0)
            except Exception:
                total = 0
            with open(tmp, "wb") as fp:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    fp.write(chunk)
                    done += len(chunk)
                    task.update({"status": "downloading",
                                 "progress": min(95, int(done * 95 / total))
                                 if total else 50,
                                 "speed": ""})
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    if done < 4096:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    task.update({"status": "processing", "progress": 97})
    filepath = tmp
    try:                                    # 归档：按标题归入同一链接的文件夹
        dest_dir = title_out_dir(title, task_id)
        dest = os.path.join(dest_dir, title + ".mp4")
        if os.path.abspath(dest) != os.path.abspath(filepath):
            if os.path.exists(dest):
                try:
                    os.remove(dest)
                except OSError:
                    pass
            shutil.move(filepath, dest)
            filepath = dest
        try:
            if not os.listdir(out_dir):
                os.rmdir(out_dir)
        except OSError:
            pass
    except OSError:
        pass
    task.update({"status": "done", "progress": 100,
                 "filepath": filepath, "filename": os.path.basename(filepath)})
    return True


# ---------------- 漫画站解析（漫画库等：章节阅读页 -> 全部页图，打包 ZIP） ----------------
# 漫画库（manhuaku.com.cn）章节阅读页的内联脚本给出总页数与图床前缀：
#   var totalPages = parseInt('21');
#   var imgBaseUrl = 'https://image1.manhuaku.com.cn/mh/<hash>/<uuid>/';
# 第 i 页即 imgBaseUrl + i + '.webp'；图床无防盗链，国内直连即可。

MANGA_RE = re.compile(
    r"^https?://(?:[\w-]+\.)*manhuaku\.com\.cn/chapter/\d+-\d+\.html", re.I)
MANGA_HOME_RE = re.compile(
    r"^https?://(?:[\w-]+\.)*manhuaku\.com\.cn/comic/\d+(\.html)?", re.I)

# 通用漫画站路径（不锁域名——这类站换域名的频率比换模板还快）：
# 包子漫画系 /comic/{id} + /comic/chapter/{id}/{s}_{c}.html；
# 其他常见 CMS：/manhua/、/manga/、/comicbook/、/chapter/...
_MANGA_GENERIC_HOME_RE = re.compile(
    r"^https?://[^/]+/(?:comic|manhua|manga|comicbook)/[^/?#\s]+", re.I)
# 注意：章节页必须含 chapter 字样；comic/manhua 本身是详情页路径，绝不能放这里
_MANGA_GENERIC_CHAPTER_RE = re.compile(
    r"^https?://[^/]+/(?:comic/)?chapters?/[^/?#\s]+", re.I)
# 已知漫画站主机/图床（用于直连判断）
_MANGA_HOSTS = ("manhuaku.com.cn", "baozimh.com", "baozimhcn.com", "cnbzmg.com",
                "bzmfcn.com", "dinnerku.com", "bzcdn.net", "klimit.cc",
                "manhuatai.com", "mkzhan.com", "kuaikanmanhua.com")

# 章节链接文字特征（中文站通用：第N话/話/回/卷 + 番外/特典/序章等）
_MANGA_CH_TEXT_RE = re.compile(
    r"第\s*[0-9〇零一二两三四五六七八九十百千万]+(?:\.\d+)?\s*[话話回卷节節]"
    r"|番外|特典|序章|序話|序话|終章|终章|尾声|完結話|外傳|外传|特別篇|特别篇")
_MANGA_CH_PATH_RE = re.compile(r"chapter|page_direct", re.I)
_MANGA_NAV_LABEL = re.compile(
    r"^[↓↑▼▲◀▶]*\s*(?:上一?[话話頁页章]|下一?[话話頁页章]|最新话|最新章节|最新話"
    r"|全部|目錄|目录|回目录|回到顶部|書架|书架|收藏|繼續閱讀|继续阅读)$")
_MANGA_JUNK_URL_RE = re.compile(
    r"logo|icon|favicon|banner|loading|avatar|qrcode|/share|/ads?[/.]|sprite"
    r"|apple-icon|android-icon|\.svg($|[?])", re.I)
_MANGA_JUNK_PATH_RE = re.compile(
    r"operation|bookmark|/search|\blogin|recommend", re.I)
from html import unescape as _html_unescape


def _is_manga_chapter(url: str) -> bool:
    """漫画章节阅读页链接（能解析出整话图片）"""
    u = (url or "").strip()
    return bool(MANGA_RE.match(u) or _MANGA_GENERIC_CHAPTER_RE.match(u))


def _is_manga(url: str) -> bool:
    """漫画站链接：章节阅读页或作品详情页（详情页给章节列表供选话）。
    通用判断按 URL 路径特征，不绑定具体域名"""
    u = (url or "").strip()
    return bool(MANGA_RE.match(u) or MANGA_HOME_RE.match(u)
                or _MANGA_GENERIC_HOME_RE.match(u)
                or _MANGA_GENERIC_CHAPTER_RE.match(u))


def _manga_site(url: str) -> bool:
    """是否漫画站页面/图床，用于强制直连（国内图床走代理必挂）"""
    m = re.match(r"^https?://([^/?#\s]+)", (url or "").strip())
    host = (m.group(1) if m else "").lower()
    if any(host == h or host.endswith("." + h) for h in _MANGA_HOSTS):
        return True
    # 漫画站经常换域名：路径特征对得上也按漫画站处理
    return _is_manga(url)


# 同一套 CMS 的镜像域名：主站挂着 WAF/换域名时，同路径内容在镜像上取
_MANGA_MIRRORS = {
    "baozimh.com": ("dinnerku.com", "www.dinnerku.com"),
}


def _manga_html(url: str) -> str:
    """漫画页抓取：直连/代理失败时自动换同系镜像域名重试"""
    try:
        return _film_html(url)
    except Exception:
        pass
    m = re.match(r"(https?://)([^/]+)(/.*)", url or "")
    if m:
        host = m.group(2).lower()
        for fam, mirrors in _MANGA_MIRRORS.items():
            if host == fam or host.endswith("." + fam):
                for mi in mirrors:
                    try:
                        return _film_html(f"{m.group(1)}{mi}{m.group(3)}")
                    except Exception:
                        continue
    raise RuntimeError(
        "漫画站无法访问（可能有反爬盾拦截，或该站域名已更换/失效）。"
        "请在浏览器确认该站当前能打开；若已换域名，用新域名链接重试")


def _js_concat_strings(expr: str) -> str:
    """求 JS 字符串拼接表达式的字面量结果（支持括号与 || 真值语义）：
    'a/' + ('' || 'b') + '/' -> 'a/b/'（空字符串是假值，|| 取第一个非空段）"""
    toks = []
    for m in re.finditer(r"'([^']*)'|\|\||[+()]", expr):
        if m.group(0) == ";":
            break
        toks.append((m.group(0), m.group(1)))
    pos = [0]

    def parse_concat():          # 字符串 + 字符串 ...，遇 ) 或 || 停
        buf = ""
        while pos[0] < len(toks):
            t, s = toks[pos[0]]
            if t in (")", "||"):
                break
            pos[0] += 1
            if t == "(":
                buf += parse_or()
                if pos[0] < len(toks) and toks[pos[0]][0] == ")":
                    pos[0] += 1
            elif t != "+":
                buf += s or ""
        return buf

    def parse_or():              # a || b || c -> 第一个非空的拼接结果
        val = parse_concat()
        while pos[0] < len(toks) and toks[pos[0]][0] == "||":
            pos[0] += 1
            nxt = parse_concat()
            if not val:
                val = nxt
        return val

    return parse_or()


def _manga_title_parts(html: str):
    """从页面里尽量猜出 (漫画名, 话名)。不绑定任何站点模板：
    标题分段里带「第N话/話/回」的是话名，其余取第一个像作品名的段。"""
    title = ""
    m = re.search(r"<title>([^<]+)</title>", html, re.I)
    if m:
        title = m.group(1).strip()
    segs = [s.strip() for s in re.split(r"\s*[-–—|]\s*", title) if s.strip()]
    # 去掉段首的表情/符号（「🍜妖神記」->「妖神記」）
    segs = [re.sub(r"^[^\w\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\u3000-\u303f]+",
                   "", s).strip() for s in segs]
    segs = [s for s in segs if s]
    chapter = ""
    name = ""
    for s in segs:
        if _MANGA_CH_TEXT_RE.search(s):
            chapter = s
            break
    if not chapter:
        m = re.search(r'data-chapter-name="([^"]+)"', html)
        if m:
            chapter = m.group(1).strip()
    # 「呪術廻戦 第271話」整段是一个标题：把作品名前缀拆出来
    mc = re.match(r"^(.+?)[\s　]*(第[0-9〇零一二两三四五六七八九十百千万]+[話话回].*)$",
                  chapter or "")
    if mc:
        chapter = mc.group(2).strip()
        name = mc.group(1).strip(" -_")
    # 漫画名：标题第一段（站点惯例「作品 - 站名」，最后一段是站名）；
    # 第一段恰是话名时，取中间段（「第271話 - 作品 - 站名」）；
    # 再不行用 h1（详情页正文标题）。h1 放最后因为它常是站点 logo 假名。
    if not name:
        if segs and segs[0] != chapter:
            name = segs[0]
        elif len(segs) >= 3:
            name = next((s for s in segs[1:-1] if s != chapter), "")
    if not name:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
        if m:
            name = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    # 「呪術廻戦漫画-呪術廻戦 番外篇」式尾部重复才去掉；名字与话名相同是
    # 「整页标题就是作品名」的正常情况，不能清空
    if chapter and name.endswith(chapter) and name != chapter:
        name = name[: -len(chapter)].strip(" -_　")
    return name.strip(), chapter.strip()


def _manga_images_from_html(html: str):
    """通用章节页图片提取：img/amp-img 的 src 与 data-src 系列属性 + JS 图片数组，
    按所在目录分组取最大的一组（一话的页图几乎总在同一目录、按数字编号），
    再按页码自然排序。过滤 logo/图标/横幅等装饰图。"""
    urls = []
    for m in re.finditer(
            r'<(?:img|amp-img)\b[^>]*?\b(?:data-src|data-original|data-lazy-src'
            r"|data-cfsrc|src)\s*=\s*[\"']([^\"']+)[\"']", html, re.I):
        urls.append(_html_unescape(m.group(1)))
    # 常见 CMS 把页图放在 JS 数组里
    for m in re.finditer(
            r"(?:imageData|chapterImages|image_list|imgData|img_list|photosr"
            r"|cp_img|imageList)\s*=\s*(\[[^\]]*\])", html, re.S | re.I):
        urls += re.findall(r"[\"'](https?://[^\"']+)[\"']", m.group(1))
    cleaned = []
    seen = set()
    for u in urls:
        u = u.strip()
        if not u.startswith("http") or u in seen:
            continue
        if _MANGA_JUNK_URL_RE.search(u) or "loading." in u:
            continue
        seen.add(u)
        cleaned.append(u)
    if not cleaned:
        return []
    # 按目录分组：一话页图同目录，装饰图各散各的
    groups = {}
    for u in cleaned:
        d = u.rsplit("/", 1)[0]
        groups.setdefault(d, []).append(u)
    best = max(groups.values(), key=len)
    # 同目录按文件名里的数字排序（1.jpg 2.jpg ... 10.jpg），纯装饰目录也会被数字排稳
    def _num(u):
        m = re.search(r"(\d+)\.[a-z0-9]+(?:$|[?#])", u.rsplit("/", 1)[-1], re.I)
        return int(m.group(1)) if m else 0
    try:
        best.sort(key=_num)
    except Exception:
        pass
    return [{"url": u, "src": u, "ext": u.rsplit(".", 1)[-1].lower()[:4]}
            for u in best if "." in u.rsplit("/", 1)[-1]]


def _manga_url_ok(u: str) -> bool:
    """页图地址结构校验：空段（//）、空格、反斜杠、无扩展名等都是拼接出错的信号。
    这类错误一旦漏出去就是「整话图片全 404」，必须在解析阶段拦住。"""
    try:
        p = urllib.parse.urlparse(u or "")
    except Exception:
        return False
    if p.scheme not in ("http", "https") or not p.netloc:
        return False
    if "//" in p.path or " " in u or "\\" in u:
        return False
    name = p.path.rsplit("/", 1)[-1]
    return bool(name) and "." in name and 1 < len(name.rsplit(".", 1)[-1]) <= 5


def _manga_probe(images, referer: str) -> bool:
    """首图探活：真下载一张，验魔数。地址拼错了（404/错误页）立刻就能发现，
    而不是等用户看到满屏裂图。失败重试一次排除瞬时抖动。"""
    if not images:
        return False
    u = images[0].get("url") or ""
    headers = {"User-Agent": UA, "Referer": referer,
               "Accept": "image/webp,image/*,*/*",
               # 只取头 2KB：验魔数足够，别为一次探活白下几 MB 整图
               "Range": "bytes=0-2047"}
    for _ in range(2):
        try:
            data = _KeepAliveFetcher(timeout=12).get(u, headers)
            if data and _is_image_bytes(data[:16]):
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def _manga_parse_chapter(url: str):
    """章节阅读页 -> (漫画名, 话名, [图片信息])。国内站，直连抓取。
    解析出图片后必须过两道校验（地址结构 + 首图探活），不达标宁可报错也不返回坏地址。"""
    html = _manga_html(url)               # 直连抓取，失败自动换镜像域名
    name, chapter = _manga_title_parts(html)

    pages, base = 0, ""
    m = re.search(r"totalPages\s*=\s*parseInt\('(\d+)'\)", html)
    if m:
        pages = int(m.group(1))
    m = re.search(r"imgBaseUrl\s*=\s*([^;\n]+)", html)
    if m:
        base = _js_concat_strings(m.group(1))
    images = []
    if pages and base:
        ms = re.search(r"imgBaseUrl\s*\+\s*i\s*\+\s*'\.(\w+)'", html)
        suffix = ms.group(1) if ms else "webp"
        images = [{"url": f"{base}{i}.{suffix}", "src": f"{base}{i}.{suffix}",
                   "ext": suffix.lower()} for i in range(1, pages + 1)]
        # 地址结构不对 -> 立刻丢弃，改走通用提取（页面里往往有真实地址）
        if not all(_manga_url_ok(i["url"]) for i in images):
            images = []
    if not images:
        # 通用兜底：img/amp-img/data-src/JS 图片数组 + 目录分组取页图
        images = [i for i in _manga_images_from_html(html)
                  if _manga_url_ok(i["url"])]
    if not images:
        raise RuntimeError(
            "manga_parse 未能从章节页解析出可用的图片地址：站点模板可能已变更")
    # 首图探活：地址拼错（404/错误页/空目录段）在这里就得暴露
    if not _manga_probe(images, url):
        alt = [i for i in _manga_images_from_html(html)
               if _manga_url_ok(i["url"]) and i["url"] != images[0]["url"]]
        if alt and _manga_probe(alt, url):
            images = alt
        else:
            raise RuntimeError(
                "manga_parse 页图地址校验失败（首张图片无法下载，可能是地址拼接变了"
                "或图床临时故障）")
    return name or "漫画", chapter, images


def _manga_file_title(name: str, chapter: str) -> str:
    """文件名「XXX漫画-XXX话」：呪術廻戦 第271話 -> 呪術廻戦漫画-第271話"""
    return f"{name}漫画-{chapter}" if chapter else f"{name}漫画"


def _manga_chapter_links(html: str, page_url: str):
    """通用章节链接提取：同站 <a>，命中「chapter 类路径」或「第N话/話/回」文字。
    包子漫画系的 /user/page_direct?comic_id=..&section_slot=..&chapter_slot=..
    跳转链在本站直接重写成 /comic/chapter/{id}/{s}_{c}.html（该路径是静态页，
    跳转目标域名反而常挂着 JS 盾）。"""
    from urllib.parse import urljoin, urlparse, parse_qs
    base_host = urlparse(page_url).netloc
    out, seen = [], set()
    for m in re.finditer(
            r'<a\b[^>]*?href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            html, re.S | re.I):
        href = _html_unescape(m.group(1).strip())
        label = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if not label or _MANGA_NAV_LABEL.match(label):
            continue
        u = urljoin(page_url, href)
        p = urlparse(u)
        if p.netloc and p.netloc != base_host:
            continue                       # 别站推荐位的外链不算章节
        if _MANGA_JUNK_PATH_RE.search(p.path):
            continue
        # 详情页形状的单段路径（/comic/{id}.html）不是章节页——漫画标题里
        # 带「第N節/序章」等字样时（如「第七節課是忍者修行！」）文字特征会误命中
        if _MANGA_GENERIC_HOME_RE.match(u) and "chapter" not in p.path.lower():
            continue
        # page_direct 跳转链才算章节；operation/bookmark 等带 chapter_slot 的不算
        if not (_MANGA_CH_PATH_RE.search(p.path)
                or ("page_direct" in p.path and "chapter_slot" in (p.query or ""))
                or _MANGA_CH_TEXT_RE.search(label)):
            continue
        if "page_direct" in p.path:
            q = parse_qs(p.query)
            cid = (q.get("comic_id") or [""])[0]
            ss = (q.get("section_slot") or ["0"])[0]
            cs = (q.get("chapter_slot") or [""])[0]
            if cid and cs:
                u = f"{p.scheme}://{p.netloc}/comic/chapter/{cid}/{ss}_{cs}.html"
        if u in seen:
            continue
        seen.add(u)
        out.append({"label": label, "url": u})
    return out


def _manga_chapters(url: str):
    """详情页（或章节页本身）-> (漫画名, [{label,url}...])。
    通用实现：直接从页面抓「第N话/話/回」类链接，不锁站点模板。"""
    from urllib.parse import urljoin
    home = re.sub(r"/chapter/(\d+)-\d+\.html", r"/comic/\1.html", url) \
        if MANGA_RE.match(url or "") else url
    html = _manga_html(home)
    name, _ch = _manga_title_parts(html)
    chapters = _manga_chapter_links(html, home)

    # 详情页常常只列最近几话 -> 抓一个章节页把全量目录合并进来
    chapter_page = url if _is_manga_chapter(url) else \
        (chapters[-1]["url"] if chapters else "")
    if chapter_page:
        try:
            ch_html = _manga_html(chapter_page)
        except Exception:
            ch_html = ""
        if ch_html:
            full = _manga_chapter_links(ch_html, chapter_page)
            if len(full) > len(chapters):
                chapters = full
    return name, chapters


def _manga_analyze(url: str) -> dict:
    """漫画解析（通用）：章节页 -> 该话页图；详情页 -> 章节列表 + 默认第一话预览。
    章节列表放 episodes（前端像影视选集一样下拉选话）。
    章节页解析失败时自动降级按详情页处理——部分站的章节 URL 形态不一定带 chapter 字样"""
    if _is_manga_chapter(url):
        try:
            name, chapter, images = _manga_parse_chapter(url)
        except Exception:
            if MANGA_RE.match(url or ""):
                raise            # manhuaku 确定的章节页，失败就是真失败
            name, chapters = _manga_chapters(url)   # 降级：当详情页给章节列表
            if not chapters:
                raise
            images, chapter = [], ""
            for ch in chapters[:3]:
                try:
                    _n, chapter, images = _manga_parse_chapter(ch["url"])
                    break
                except Exception:
                    continue
            if not images:
                raise
            title = f"{name}漫画" if name else _manga_file_title(name, chapter)
        else:
            title = _manga_file_title(name, chapter)
            chapters = _manga_chapters(url)[1]
    else:
        name, chapters = _manga_chapters(url)
        if not chapters:
            raise RuntimeError("manga_parse 详情页未找到任何章节链接：页面结构可能已变更")
        images, chapter = [], ""
        for ch in chapters[:3]:        # 个别章节页偶发结构差异，多试两话
            try:
                _n, chapter, images = _manga_parse_chapter(ch["url"])
                break
            except Exception:
                continue
        if not images:
            raise RuntimeError("manga_parse 未能从章节页解析出图片列表：页面结构可能已变更")
        title = f"{name}漫画" if name else _manga_file_title(name, chapter)
    host = re.match(r"^https?://([^/?#\s]+)", url or "")
    site = re.sub(r"^(www|cn|m)\.", "", (host.group(1) if host else "").lower())
    return {
        "title": title, "author": name or "漫画",
        "cover": images[0]["url"],
        "covers": [i["url"] for i in images[:3]],
        "duration": "--:--",
        "platform": site, "webpage": url,
        "resolutions": [], "views": None, "is_live": False,
        "content_type": "image", "image_count": len(images),
        "images": images[:24], "ext": images[0]["ext"],
        # 多话漫画：章节列表（前端下拉选话，下载时把所选话的阅读页带回）
        "episodes": chapters if len(chapters) > 1 else [],
        "preview_url": "", "preview_silent": False,
        "preview_audio_url": "",
    }


class _KeepAliveFetcher:
    """直连 + 每线程复用一条 HTTPS 长连接（专属漫画图床）。
    每页新建连接时 TLS 握手占了大头：实测 53 页 70.5s -> 29.3s，且失败归零。
    走 urllib 的 ProxyHandler({}) 不建代理，这里同样不读系统代理（国内图床挂代理必挂）。"""

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self._tl = threading.local()

    def _conn(self, host: str):
        c = getattr(self._tl, "conn", None)
        if c is None or getattr(self._tl, "host", "") != host:
            c = http.client.HTTPSConnection(host, timeout=self.timeout)
            self._tl.conn, self._tl.host = c, host
        return c

    def get(self, url: str, headers: dict) -> bytes:
        p = urllib.parse.urlparse(url)
        # 请求行只能是 ASCII：路径/查询里的中文（如「1.姐姐1」）必须先百分号转义，
        # 否则 http.client 直接抛 UnicodeEncodeError，整话图片全下不来
        path = urllib.parse.quote(p.path, safe="/%:@!$&'()*+,;=-._~")
        query = urllib.parse.quote(p.query, safe="=&%:@!$'()*+,;/?-._~")
        target = path + (("?" + query) if query else "")
        last = None
        for attempt in range(2):
            c = self._conn(p.netloc)
            try:
                c.request("GET", target, headers=headers)
                r = c.getresponse()
                data = r.read()
                if r.status not in (200, 206):     # 206 = Range 请求的分段响应
                    raise RuntimeError(f"HTTP {r.status}")
                return data
            except Exception as e:
                last = e
                try:
                    c.close()
                except Exception:
                    pass
                self._tl.conn = None
                if attempt == 0:
                    time.sleep(0.15)      # 连接坏了立刻重连一次，不再整轮 sleep
        raise last or RuntimeError("fetch failed")


def _convert_png_quiet(path: str):
    """转 PNG + 小图放大；失败（损坏页）删掉算失败，不抛异常。
    刚写完的文件可能被杀软短暂锁定（首次 Image.open 报权限错）：稍候重试一次"""
    for attempt in range(2):
        try:
            return _convert_image_png(path)
        except Exception:
            if attempt == 0:
                time.sleep(0.6)
                continue
    try:
        os.remove(path)
    except OSError:
        pass   # 删不掉（仍被占用）就先留着：断点续传按 PNG 张数判断会重新下载
    return None


def _silent_remove(path: str) -> bool:
    """删文件，撞上杀软/索引器短暂占用就重试几次；删不掉也不抛错"""
    for _ in range(3):
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(0.05)
    return False


def _save_image_bytes_png(data: bytes, dst: str) -> None:
    """内存里的图片字节 -> 高清 PNG（顺带小图放大）。
    全程不落中间文件：批量下载时 webp/jpg 原图一个都不会留在输出目录。
    解不出来（截断/错误页/罕见格式）抛错，由调用方把这一页算失败。"""
    try:
        import io as _io
        from PIL import Image
        with Image.open(_io.BytesIO(data)) as im:
            if getattr(im, "n_frames", 1) > 1:
                raise RuntimeError("animated")   # 动图交给 ffmpeg 取首帧
            im.load()
            im = _upscale_if_small(im)
            im.save(dst, "PNG", compress_level=3)
        return
    except ImportError:
        pass
    except Exception:
        _silent_remove(dst)      # 半成品 PNG 不能留
    tmp = dst + ".tmpimg"
    try:
        with open(tmp, "wb") as fp:
            fp.write(data)
        subprocess.run([FFMPEG, "-y", "-i", tmp, dst],
                       capture_output=True, timeout=30)
        if os.path.exists(dst) and os.path.getsize(dst) > 1024:
            return
    except Exception:
        pass
    finally:
        _silent_remove(tmp)
        if os.path.exists(dst) and os.path.getsize(dst) <= 1024:
            _silent_remove(dst)
    raise RuntimeError("图片无法转 PNG")


_LEGACY_IMG_EXT = (".webp", ".jpg", ".jpeg", ".jfif", ".gif",
                   ".avif", ".bmp", ".tif", ".tiff")


def _chapter_png_only(out_dir: str) -> int:
    """把一话目录里的非 PNG 图片清干净：能转的转成同名 PNG（已有 PNG 就直接删源），
    转不出来的是坏图也删掉。保证输出目录里只剩 PNG 散图——即使用户用的是
    旧版本留下的目录（里面混着 webp），重下一次也会被就地收拾干净。"""
    try:
        names = os.listdir(out_dir)
    except OSError:
        return 0
    cleaned = 0
    for f in names:
        if not f.lower().endswith(_LEGACY_IMG_EXT):
            continue
        src = os.path.join(out_dir, f)
        dst = os.path.splitext(src)[0] + ".png"
        try:
            if os.path.exists(dst) and os.path.getsize(dst) > 1024:
                _silent_remove(src)
                cleaned += 1
                continue
            for attempt in range(2):
                try:
                    _convert_image_png(src)      # 成功会自己删掉源文件
                    cleaned += 1
                    break
                except Exception:
                    if attempt == 0:
                        time.sleep(0.3)
            else:
                _silent_remove(src)              # 转不出来的坏图，不留垃圾
                cleaned += 1
        except OSError:
            pass
    return cleaned


def _chapter_png_count(out_dir: str) -> int:
    try:
        return sum(1 for f in os.listdir(out_dir) if f.lower().endswith(".png"))
    except OSError:
        return 0


def _pack_manga(task_id: str, title: str, images, webpage: str,
                parent_dir: str = "", on_progress=None):
    """漫画整话下载：页图国内直连 + 线程池并发下载（逐张串行太慢），
    全部统一转高清 PNG 后逐张存放（不打包 ZIP）。
    parent_dir：批量下载时把该话放进总文件夹下的二级子文件夹；
    on_progress：批量模式传入回调(done, total)接管进度上报，任务终态由批量调用方负责"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    task = TASKS[task_id]
    out_dir = (os.path.join(parent_dir, title) if parent_dir
               else title_out_dir(title, task_id))
    os.makedirs(out_dir, exist_ok=True)
    total = len(images)
    if not on_progress:
        task.update({"status": "downloading", "progress": 0, "total": total,
                     "count": f"0/{total}"})

    # 旧版本中断留下的 webp/jpg 先收拾干净（已有同名 PNG 的直接删，能转的就地转）
    _chapter_png_only(out_dir)

    # 强制直连 + 连接复用：漫画图床走代理连不上，反复握手又太慢
    fetcher = _KeepAliveFetcher()
    lock = threading.Lock()
    state = {"done": 0, "ok": 0, "skip": 0, "errs": []}

    def work(i, img):
        """一页：下载字节 -> 内存转 PNG 落盘。不写中间文件，避免线程间交接
        未转换的原图（旧实现的 webp 残留就是这么来的）。已有 PNG 直接跳过。"""
        dst = os.path.join(out_dir, f"{title}_{i:03d}.png")
        if os.path.exists(dst) and os.path.getsize(dst) > 1024:
            return "skip"
        urls = [u for u in dict.fromkeys([img.get("url"), img.get("src")]) if u]
        headers = {"User-Agent": UA, "Referer": webpage,
                   "Accept": "image/webp,image/*,*/*"}
        last = None
        for attempt in range(3):
            for u in urls:
                try:
                    data = fetcher.get(u, headers)
                    # 校验图片魔数：CDN 错误页/截断响应不算成功，换备用地址重试
                    if not _is_image_bytes(data[:16]):
                        raise RuntimeError("响应不是图片（可能被限流或已截断）")
                    _save_image_bytes_png(data, dst)
                    return "ok"
                except Exception as e:
                    last = e
                    _silent_remove(dst)          # 半成品绝不留
            time.sleep(0.6 * (attempt + 1))      # 页与页错峰
        raise last or RuntimeError("fetch failed")

    # 4 线程 + 长连接是实测最优：8 线程反而更慢且失败率上升（图床按 IP 限速）
    with ThreadPoolExecutor(max_workers=min(4, max(1, total))) as pool:
        futures = {pool.submit(work, i, img): i
                   for i, img in enumerate(images, 1)}
        for fut in as_completed(futures):
            try:
                k = fut.result()
                with lock:
                    state[k] += 1
            except Exception as e:
                with lock:
                    state["errs"].append(f"第{futures[fut]}页 {e}")
            with lock:
                state["done"] += 1
                done_now = state["done"]
            if on_progress:
                on_progress(done_now, total)
            else:
                task.update({"status": "downloading",
                             "progress": int(done_now * 100 / total),
                             "count": f"{done_now}/{total}"})

    # 收尾再扫一遍：并发/中断期间可能还有残留，保证这一话只剩 PNG
    _chapter_png_only(out_dir)
    got = _chapter_png_count(out_dir)
    if got == 0:
        detail = "；".join(state["errs"][-3:])[:200]
        raise RuntimeError("network_error 漫画页图全部下载失败（" + detail + "）")

    if on_progress:
        return got
    task.update({"status": "done", "progress": 100,
                 "count": f"{got}/{total}",
                 "filepath": out_dir,
                 "filename": os.path.basename(out_dir)})


def run_manga(task_id: str, url: str, chapter_url: str = ""):
    """漫画下载：解析所选话（chapter_url）或详情页默认第一话的全部页图
    -> 并发下载 -> 统一 PNG 散图存放（不打包 ZIP）"""
    task = TASKS[task_id]
    try:
        target = chapter_url if _is_manga_chapter(chapter_url) else url
        if not _is_manga_chapter(target):
            _n, chapters = _manga_chapters(url)
            if not chapters:
                raise RuntimeError("manga_parse 未找到任何章节链接")
            target = chapters[0]["url"]
        name, chapter, images = _manga_parse_chapter(target)
        _pack_manga(task_id, sanitize(_manga_file_title(name, chapter)),
                    images, target)
    except Exception as e:
        code = classify_error(str(e))
        task.update({"status": "error",
                     "error": ZH_ERRORS.get(code, "下载失败"),
                     "detail": str(e)[:300],
                     "error_code": code})


# ---------------- 路由 ----------------

def classify_error(msg: str) -> str:
    """将底层异常归类为稳定的错误码，供前端做国际化翻译"""
    low = msg.lower()
    if "xhs_parse" in low:
        return "XHS"
    if "manga_parse" in low:
        return "MANGA_PARSE"
    if "qishui_no_audio" in low:
        return "QISHUI_NO_AUDIO"
    if "qishui_parse" in low:
        return "QISHUI_PARSE"
    if "film_source" in low:
        return "FILM_SOURCE"
    if "film_parse" in low:
        return "FILM_PARSE"
    # 爱奇艺短链过期/内容被删：分享短链 302 到 status-404 页，提取器报 Can't find any video
    if "can't find any video" in low and ("iqiyi" in low or "qy.net" in low):
        return "IQIYI_PARSE"
    # X（推特）对未登录访客隐藏内容：报「Video N is unavailable」实为需要登录
    if "unavailable" in low and ("twitter" in low or "video #" in low):
        return "X_LOGIN"
    for dc in ("douyin_cdp_timeout", "douyin_no_browser", "douyin_browser_fail",
               "douyin_no_ws", "douyin_parse", "douyin_tab_fail"):
        if dc in low:
            return dc.upper()
    if "weixin_sph" in low or "微信视频号" in msg:
        return "WEIXIN_SPH"
    if ("could not copy" in low or "cookie database" in low or "esentutl" in low
            or "database is locked" in low or "permission denied" in low):
        return "COOKIE_LOCK"
    if "dpapi" in low or "app-bound" in low or "app_bound" in low:
        return "COOKIE_ENCRYPT"
    # YouTube 的 IP 风控：换了客户端也拦，给用户专门的解释（换节点 / Cookie）
    if "not a bot" in low or "sign in to confirm" in low:
        return "YT_BOT"
    if ("login" in low or "registered users" in low or "premium" in low
            or "members-only" in low or "vip" in low
            or "会员" in msg or "大会员" in msg):
        return "SIGNIN"
    if "sign in" in low or "not a bot" in low or "cookies" in low:
        return "SIGNIN"
    if ("no video could be found" in low or "no media" in low
            or "no formats found" in low or "no video formats" in low
            or "did not get any data" in low or "no audio" in low and "no video" in low):
        return "NO_MEDIA"
    if ("private" in low or "has been deleted" in low or "was deleted" in low
            or "removed by" in low or "not available" in low or "does not exist" in low
            or "http error 404" in low):
        return "PRIVATE"
    if "proxyerror" in low or "tunnel connection failed" in low or "proxy" in low:
        return "PROXY"
    if "proxy_needed" in low:
        return "PROXY_NEEDED"
    if "network_error" in low:
        return "NETWORK"
    if ("ssl" in low or "certificate" in low or "handshake" in low
            or "unexpected eof" in low or "connection was reset" in low
            or "connection actively refused" in low or "目标计算机积极拒绝" in msg
            or "远程计算机" in msg):
        return "NETWORK"
    if ("timed out" in low or "timeout" in low or "unable to download webpage" in low
            or "getaddrinfo" in low or "name or service not known" in low
            or "connection refused" in low or "connection reset" in low
            or "connection aborted" in low or "network is unreachable" in low
            or "err_name_not_resolved" in low or "err_connection" in low
            or "http error 403" in low or "http error 429" in low
            or "temporary failure" in low or "unreachable" in low
            or "winerror 10060" in low or "winerror 10061" in low
            or "transport" in low
            # Windows 中文系统下的连接错误（非英文关键字）
            or "目标计算机积极拒绝" in msg or "无法连接" in msg
            or "连接超时" in msg or "远程主机强迫关闭" in msg
            or "由于连接方在一段时间后没有正确答复" in msg):
        return "NETWORK"
    if "unsupported url" in low:
        return "UNSUPPORTED"
    return "GENERIC"


def _is_cookie_error(msg: str) -> bool:
    """判断是否为「浏览器 Cookie 读取失败」类错误（可降级重试）"""
    return classify_error(msg) == "COOKIE_LOCK"


def _needs_login(msg: str) -> bool:
    """判断失败是否因为「需要登录 / 会员权限」——只有这类才值得用浏览器登录态重试"""
    return classify_error(msg) in ("SIGNIN", "COOKIE_LOCK", "COOKIE_ENCRYPT")


# 微信视频号链接（视频流只在微信客户端内下发，网页接口需要元宝签发的 exportId/token）
SPH_RE = re.compile(
    r"^https?://(www\.)?weixin\.qq\.com/sph/|^https?://channels\.weixin\.qq\.com", re.I)

# ---------------------------------------------------------------------------
# 微信视频号 / WeChat Channels
# 链路参考 ltaoo/wx_channels_download 的 sph worker 与 datawhalechina/video-devour
# （MIT）：腾讯元宝 get_parse_result 换 exportId+token → 视频号 finder-preview
# get_feed_info 取媒体直链 → 带 decodeKey 时按 ISAAC64 解密前 128KB。
# 元宝接口需要一次登录后的 Cookie，存在程序目录的 wechat_yuanbao_cookie.txt。
# ---------------------------------------------------------------------------
SPH_COOKIE_FILE = os.path.join(BASE_DIR, "wechat_yuanbao_cookie.txt")
_SPH_ENC_LIMIT = 131072
_U64 = (1 << 64) - 1
_SPH_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
_SPH_YUANBAO_API = "https://yuanbao.tencent.com/api/weixin/get_parse_result"
_SPH_FEED_API = "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"


class SphError(RuntimeError):
    """视频号链路里可直接展示给用户的错误（code 供前端翻译）"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def _sph_cookie() -> str:
    try:
        with open(SPH_COOKIE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _sph_save_cookie(cookie: str) -> None:
    cookie = (cookie or "").strip()
    if cookie:
        with open(SPH_COOKIE_FILE, "w", encoding="utf-8") as f:
            f.write(cookie)
    elif os.path.exists(SPH_COOKIE_FILE):
        os.remove(SPH_COOKIE_FILE)


def _sph_http(url: str, payload: dict, headers: dict, proxy: str = "",
              timeout: int = 30) -> dict:
    """POST JSON（可选代理），返回解析后的 JSON；401/403 单独抛出"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    handlers = []
    if proxy and proxy != "direct":
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        body = e.read()
        if e.code in (401, 403):
            raise SphError("SPH_COOKIE", "腾讯元宝 Cookie 无效或已过期（HTTP %d）" % e.code)
        raise SphError("SPH_NET", f"HTTP {e.code}: {body[:200]!r}")
    except Exception as e:
        raise SphError("SPH_NET", str(e)[:200])
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        raise SphError("SPH_NET", "接口返回的不是 JSON：" + body[:120].decode("utf-8", "replace"))


def _sph_share_id(url: str) -> str:
    m = re.search(r"/sph/([A-Za-z0-9_-]+)", url or "")
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]+)", url or "")
    return m.group(1) if m else ""


def _sph_normalize_url(url: str) -> str:
    sid = _sph_share_id(url)
    return f"https://weixin.qq.com/sph/{sid}" if sid else url


def _sph_yuanbao_parse(share_url: str, proxy: str = "") -> tuple:
    """元宝解析 → (export_id, token)"""
    cookie = _sph_cookie()
    if not cookie:
        raise SphError("SPH_COOKIE", "no cookie")
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "content-type": "application/json",
        "origin": "https://yuanbao.tencent.com",
        "referer": "https://yuanbao.tencent.com/",
        "user-agent": _SPH_UA,
        "x-requested-with": "XMLHttpRequest",
        "x-source": "web",
        "x-platform": "windows",
        "x-language": "zh-CN",
        "cookie": cookie,
    }
    data = _sph_http(_SPH_YUANBAO_API,
                     {"type": "video_channel_url", "url": share_url, "scene": 1},
                     headers, proxy).get("data") or {}
    export_id = data.get("wx_export_id") or ""
    token = ""
    playable = data.get("playable_url") or ""
    if playable:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(playable).query)
        token = (q.get("token") or [""])[0]
        export_id = (q.get("eid") or [export_id])[0]
    if not export_id:
        raise SphError("SPH_PARSE", "元宝未返回 export id（分享链接可能已失效）")
    return export_id, token


def _sph_feed_info(eid: str, token: str, proxy: str = "") -> dict:
    rid = f"{int(time.time()):x}-" + "".join(
        random.choice("0123456789abcdef") for _ in range(8))
    url = (f"{_SPH_FEED_API}?_rid={rid}"
           "&_pageUrl=https%3A%2F%2Fchannels.weixin.qq.com%2Ffinder-preview%2Fpages%2Ffeed")
    referer = ("https://channels.weixin.qq.com/finder-preview/pages/feed"
               f"?entry_card_type=48&comment_scene=39&appid=0"
               f"&token={urllib.parse.quote(token)}&entry_scene=0"
               f"&eid={urllib.parse.quote(eid)}")
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "Origin": "https://channels.weixin.qq.com",
        "Referer": referer,
        "User-Agent": _SPH_UA,
    }
    result = _sph_http(url, {"baseReq": {"generalToken": token}, "exportId": eid},
                       headers, proxy)
    if result.get("errCode"):
        raise SphError("SPH_PARSE", re.sub(r"<[^>]+>", "", str(result.get("errMsg") or ""))[:160])
    detail = (result.get("data") or {}).get("errMsg") or {}
    if detail:
        msg = re.sub(r"<[^>]+>", "", str(detail.get("content") or detail.get("title") or ""))
        # 成功响应里也会带 errMsg 状态对象（如 {"type": 0}），只有出现
        # 实质错误文案或非 0 的 type 才是真正的失败
        etype = detail.get("type")
        if msg or etype not in (None, 0, "0"):
            raise SphError("SPH_PARSE", msg[:160] or "内容无法播放（可能是回放或已下架）")
    return result


def _sph_collect_media(node, found=None) -> list:
    """递归挑出媒体项：{url, decode_key, size_hint}"""
    if found is None:
        found = []
    if isinstance(node, dict):
        url = ""
        for key in ("mediaUrl", "videoUrl", "url"):
            v = node.get(key)
            if isinstance(v, str) and v.startswith(("http://", "https://")):
                url = v
                break
        if url and any(k in node for k in
                       ("decodeKey", "fileType", "mediaUrl", "videoUrl", "spec")):
            tok = node.get("urlToken") or ""
            if tok and tok not in url:
                url += tok
            try:
                dk = int(str(node.get("decodeKey"))) if node.get("decodeKey") not in (None, "") else None
            except (TypeError, ValueError):
                dk = None
            size = 0
            for k in ("fileSize", "size", "videoSize"):
                try:
                    size = int(node.get(k) or 0)
                except (TypeError, ValueError):
                    size = 0
                if size:
                    break
            found.append({"url": url, "decode_key": dk, "size": size})
        for v in node.values():
            _sph_collect_media(v, found)
    elif isinstance(node, list):
        for v in node:
            _sph_collect_media(v, found)
    return found


def _sph_meta(feed: dict) -> dict:
    meta = {"title": "", "author": "", "cover": ""}

    def walk(node):
        if isinstance(node, dict):
            if not meta["title"] and node.get("description"):
                meta["title"] = re.sub(r"<[^>]+>", "", str(node["description"]))[:120]
            if not meta["author"] and node.get("nickname"):
                meta["author"] = str(node["nickname"])
            if not meta["cover"]:
                for k in ("coverUrl", "coverImgUrl", "thumbUrl"):
                    if node.get(k):
                        meta["cover"] = str(node[k])
                        break
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(feed)
    return meta


def _sph_resolve(url: str, proxy: str = "") -> dict:
    """分享链接 → 标题/作者/封面/媒体列表（国内直连优先，失败再走代理）"""
    sid = _sph_share_id(url)
    if not sid:
        raise SphError("SPH_PARSE", "没有识别到视频号 id")
    share_url = _sph_normalize_url(url)
    attempts = [""] + ([proxy] if proxy and proxy != "direct" else [])
    last = None
    for p in attempts:
        try:
            eid, token = _sph_yuanbao_parse(share_url, p)
            feed = _sph_feed_info(eid, token, p)
            break
        except SphError as e:
            last = e
            if e.code != "SPH_NET":      # Cookie / 解析错误换代理也没用
                raise
    else:
        raise last or SphError("SPH_NET", "解析失败")
    media = [m for m in _sph_collect_media(feed) if ".m3u8" not in m["url"].lower()]
    if not media:
        raise SphError("SPH_PARSE", "接口没有返回可下载的视频地址")
    media.sort(key=lambda m: m["size"], reverse=True)
    meta = _sph_meta(feed)
    return {
        "id": sid,
        "title": meta["title"] or "微信视频号视频",
        "author": meta["author"] or "微信视频号",
        "cover": meta["cover"],
        "media": media[0],
        "webpage": _sph_normalize_url(url),
    }


def analyze_sph(url: str, proxy: str = "") -> dict:
    """视频号解析结果，字段与 yt-dlp 通道保持一致"""
    info = _sph_resolve(url, proxy)
    return {
        "title": info["title"],
        "author": info["author"],
        "cover": info["cover"],
        "covers": [info["cover"]] if info["cover"] else [],
        "duration": "--:--",
        "platform": "WeChat Channels",
        "webpage": info["webpage"],
        "resolutions": ["原画"],
        "views": None,
        "is_live": False,
        "content_type": "video",
        "image_count": 0,
        "images": [],
        "ext": "mp4",
        "preview_url": info["media"]["url"],
        "preview_silent": False,
        "preview_audio_url": "",
        "cookie_used": "yuanbao",
    }


def _isaac64_mix(a, b, c, d, e, f, g, h):
    a = (a - e) & _U64
    f ^= h >> 9
    h = (h + a) & _U64
    b = (b - f) & _U64
    g ^= (a << 9) & _U64
    a = (a + b) & _U64
    c = (c - g) & _U64
    h ^= b >> 23
    b = (b + c) & _U64
    d = (d - h) & _U64
    a ^= (c << 15) & _U64
    c = (c + d) & _U64
    e = (e - a) & _U64
    b ^= d >> 14
    d = (d + e) & _U64
    f = (f - b) & _U64
    c ^= (e << 20) & _U64
    e = (e + f) & _U64
    g = (g - c) & _U64
    d ^= f >> 17
    f = (f + g) & _U64
    h = (h - d) & _U64
    e ^= (g << 14) & _U64
    g = (g + h) & _U64
    return a, b, c, d, e, f, g, h


def _isaac64_keystream(key: int):
    """WechatSphDecrypt 的 ISAAC64 密钥流（每个数 XOR 8 字节，大端）"""
    golden = 0x9E3779B97F4A7C13
    seed = [0] * 256
    seed[0] = key & _U64
    mm = [0] * 256
    a = b = c = d = e = f = g = h = golden
    for _ in range(4):
        a, b, c, d, e, f, g, h = _isaac64_mix(a, b, c, d, e, f, g, h)
    for i in range(0, 256, 8):
        a = (a + seed[i]) & _U64
        b = (b + seed[i + 1]) & _U64
        c = (c + seed[i + 2]) & _U64
        d = (d + seed[i + 3]) & _U64
        e = (e + seed[i + 4]) & _U64
        f = (f + seed[i + 5]) & _U64
        g = (g + seed[i + 6]) & _U64
        h = (h + seed[i + 7]) & _U64
        a, b, c, d, e, f, g, h = _isaac64_mix(a, b, c, d, e, f, g, h)
        mm[i:i + 8] = [a, b, c, d, e, f, g, h]
    for i in range(0, 256, 8):
        a = (a + mm[i]) & _U64
        b = (b + mm[i + 1]) & _U64
        c = (c + mm[i + 2]) & _U64
        d = (d + mm[i + 3]) & _U64
        e = (e + mm[i + 4]) & _U64
        f = (f + mm[i + 5]) & _U64
        g = (g + mm[i + 6]) & _U64
        h = (h + mm[i + 7]) & _U64
        a, b, c, d, e, f, g, h = _isaac64_mix(a, b, c, d, e, f, g, h)
        mm[i:i + 8] = [a, b, c, d, e, f, g, h]

    state = {"aa": 0, "bb": 0, "cc": 0}

    def refill():
        state["cc"] = (state["cc"] + 1) & _U64
        state["bb"] = (state["bb"] + state["cc"]) & _U64
        aa, bb = state["aa"], state["bb"]
        for i in range(256):
            if i % 4 == 0:
                aa = ~(aa ^ ((aa << 21) & _U64)) & _U64
            elif i % 4 == 1:
                aa = (aa ^ (aa >> 5)) & _U64
            elif i % 4 == 2:
                aa = (aa ^ ((aa << 12) & _U64)) & _U64
            else:
                aa = (aa ^ (aa >> 33)) & _U64
            aa = (aa + mm[(i + 128) % 256]) & _U64
            x = mm[i]
            y = (mm[(x >> 3) % 256] + aa + bb) & _U64
            mm[i] = y
            bb = (mm[(y >> 11) % 256] + x) & _U64
            seed[i] = bb
        state["aa"], state["bb"] = aa, bb

    refill()
    cnt = 255
    while True:
        result = seed[cnt]
        if cnt == 0:
            refill()
            cnt = 255
        else:
            cnt -= 1
        yield result


def _sph_decrypt_head(path: str, key: int, enc_len: int = _SPH_ENC_LIMIT) -> None:
    span = min(os.path.getsize(path), enc_len) // 8 * 8
    if span <= 0:
        return
    with open(path, "r+b") as f:
        head = f.read(span)
        out = bytearray(span)
        i = 0
        for rnd in _isaac64_keystream(key):
            if i >= span:
                break
            stream = rnd.to_bytes(8, "big")
            for j in range(8):
                if i + j < span:
                    out[i + j] = head[i + j] ^ stream[j]
            i += 8
        f.seek(0)
        f.write(bytes(out))


def run_sph(task_id: str, url: str, kind: str = "video", proxy: str = "") -> None:
    """视频号下载：解析 → 流式下载 → 需要时解密首段 → 收进标题文件夹"""
    task = TASKS[task_id]
    try:
        info = _sph_resolve(url, proxy)
        media = info["media"]
        out_dir = title_out_dir(info["title"], task_id)
        path = os.path.join(out_dir, f"{sanitize(info['title'])}.mp4")
        task.update({"status": "downloading", "progress": 0, "speed": "", "eta": 0})
        headers = {"User-Agent": _SPH_UA, "Referer": "https://channels.weixin.qq.com/"}
        last_err = None
        for p in [""] + ([proxy] if proxy and proxy != "direct" else []):
            try:
                req = urllib.request.Request(media["url"], headers=headers)
                handlers = []
                if p:
                    handlers.append(urllib.request.ProxyHandler({"http": p, "https": p}))
                opener = urllib.request.build_opener(*handlers)
                with opener.open(req, timeout=60) as r, open(path, "wb") as fp:
                    total = int(r.headers.get("Content-Length") or media.get("size") or 0)
                    done = 0
                    while True:
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        fp.write(chunk)
                        done += len(chunk)
                        if total:
                            task.update({"status": "downloading",
                                         "progress": min(99, int(done * 100 / total)),
                                         "speed": "", "eta": 0})
                last_err = None
                break
            except Exception as e:
                last_err = e
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        if last_err:
            raise SphError("SPH_NET", str(last_err)[:160])
        if not os.path.exists(path) or os.path.getsize(path) < 1024:
            raise SphError("SPH_NET", "下载到的文件为空")
        if media.get("decode_key"):
            task.update({"status": "processing", "progress": 99})
            _sph_decrypt_head(path, media["decode_key"])
        task.update({"status": "done", "progress": 100,
                     "filepath": path, "filename": os.path.basename(path)})
    except SphError as e:
        task.update({"status": "error", "error": e.code, "detail": e.detail,
                     "error_code": e.code})
    except Exception as e:
        task.update({"status": "error", "error": str(e)[:200],
                     "detail": str(e)[:300], "error_code": "SPH_NET"})



# 分享文案里混着口令尾巴（如「... 07/22 hBt:/ 3pm J@V.LW」），只取真正的链接
URL_RE = re.compile(r"https?://[^\s<>\"'，。、；！？）)\]]+", re.I)
# scheme-less links that show up in share texts, e.g. v.douyin.com/xxxx
BARE_URL_RE = re.compile(
    r"(?:[\w-]+\.)+(?:com|cn|co|tv|net|org)(?:/[^\s<>\"'，。、；！？）)\]]+)?",
    re.I)


def extract_url(text: str) -> str:
    """从粘贴的分享文案中提取第一条链接，并去掉结尾的标点"""
    text = (text or "").strip()
    m = URL_RE.search(text)
    if m:
        return m.group(0).rstrip(".,;:!?、。，；！？")
    m = BARE_URL_RE.search(text)
    if m:
        return m.group(0).rstrip(".,;:!?、。，；！？")
    # No URL at all: return empty so the caller can show a helpful error,
    # instead of feeding the whole Chinese sentence to yt-dlp as a "host"
    # (which crashes with an idna codec error and a useless message).
    return ""

ZH_ERRORS = {
    "IQIYI_PARSE": "这条爱奇艺链接已失效：短链被平台重定向到 404 页面（内容可能已被删除、下架，或分享已过期）。请在爱奇艺 App 里重新分享一次拿新链接；VIP/付费内容需要登录会员账号才能观看，工具不支持解析付费内容",
    "QISHUI_NO_AUDIO": "平台未提供这首歌的音频源（因版权调整暂不可用，或下载需要平台会员），任何工具都无法下载。这不是工具或链接的问题：可在汽水音乐 App 里确认该歌曲是否还能播放",
    "QISHUI_PARSE": "汽水音乐页面数据获取失败：分享链接可能不完整或已失效，请在汽水音乐 App 重新分享一次",
    "FILM_PARSE": "未能从该影视页面解析出视频源：播放线路可能已失效，或链接不是影片详情页/播放页。请在原站打开影片、换一条线路播放，再复制页面链接重试",
    "FILM_SOURCE": "影片视频源拉取失败：影片服务器暂时无法访问，请稍后重试",
    "MANGA_PARSE": "未能从该漫画章节页解析出图片：链接可能已失效，或不是某一话的阅读页。请在漫画站打开该话的阅读页（一话的完整阅读页面），复制浏览器地址栏链接重试",
    "WEIXIN_SPH": "微信视频号的内容受微信客户端授权保护（数据需微信内部签发的 exportkey 才能获取），无法匿名解析。建议：若该视频也发布在抖音/B站/YouTube 等平台，请改用对应平台的链接；或在微信中播放时录屏保存",
    "COOKIE_LOCK": "该内容需要登录，但浏览器登录态暂时读不到（浏览器运行时会锁住 Cookie 数据库）。点「一键导出」即可自动处理：关闭浏览器 → 导出 → 自动重启并恢复标签页，之后永久生效",
    "COOKIE_ENCRYPT": "Edge / Chrome 新版启用了 App-Bound 加密，第三方工具无法解密其 Cookie（官方安全限制，所有下载工具都受影响）。方案：① 用 Firefox 登录该平台后点「一键导出」；② 在 Edge 装扩展「Get cookies.txt LOCALLY」导出 txt，再点「导入」",
    "SIGNIN": "这条内容需要登录才能解析（工具已自动尝试你本机浏览器的登录态）。请先用浏览器登录该平台，再点「一键导出」生成 Cookie 文件，之后永久生效、无需再操作",
    "X_LOGIN": "X（推特）的访客通道不稳定，偶尔会把视频误报为「不可用」（工具已自动重试仍失败）。请稍等几秒再点一次「开始解析」，通常即可成功；反复失败可换一个代理节点试试。若长期无法解析，再用「粘贴」配置已登录 X 的 Cookie 作保底（一次配置，永久生效）",
    "PROXY": "当前代理无法访问该平台。请更换「代理设置」中的代理地址（如 http://127.0.0.1:7897），或点击「测试」验证",
    "PROXY_NEEDED": "解析失败：该平台的视频/图片服务器无法直连（当前未填写代理）。请在「代理设置」填入代理软件的本地端口，或点「自动检测」自动填入后重试",
    "NETWORK": "网络无法访问该平台：解析海外平台时，多半是当前代理节点不通或被限制——请更换代理节点后重试，也可点「自动检测」挑选可用节点；解析国内平台时请清空代理直连",
    "NO_MEDIA": "这条链接里没有找到可提取的视频/图集/音频：可能只是普通文字帖，或媒体已被删除。X/推特的图片帖会自动走图集通道解析；若链接确实带图/带视频仍报此错，可能是平台接口临时受限，请稍后重试或更换代理节点",
    "PRIVATE": "该内容不存在、已删除或被设为私密，无法解析。请确认原链接在浏览器里是否还能正常打开",
    "UNSUPPORTED": "暂不支持该链接所属的平台，或链接格式有误",
    "XHS": "未能解析该小红书笔记：链接可能已失效、内容已被删除，或需要在小红书 App 内查看。普通公开的图文/视频笔记无需登录即可解析，可尝试重新复制分享链接",
    "DOUYIN_CDP_TIMEOUT": "抖音解析超时：已通过本机浏览器打开该页面，但迟迟未取到视频数据——页面可能弹出了滑块验证，请稍后重试",
    "DOUYIN_NO_BROWSER": "解析抖音需要本机安装 Edge 或 Chrome 浏览器（会以无痕模式在后台打开页面取数据，全程免登录），但未能找到浏览器。请安装 Edge/Chrome 后重试",
    "DOUYIN_PARSE": "未能解析该抖音作品：链接可能已失效、内容已被删除，或复制时少了字符（链接区分大小写）。请在抖音 App 里重新分享一次，复制完整链接后重试",
    "DOUYIN_NO_WS": "缺少解析抖音所需的 websocket-client 组件，请在命令行执行 pip install websocket-client 后重试",
    "DOUYIN_TAB_FAIL": "抖音浏览器通道启动失败，请重试；若反复出现，请关闭后台多余的 Edge/Chrome 窗口后重试",
    "GENERIC": "解析失败",
}

# ---------------- 小红书专用解析（不依赖 yt-dlp，公开笔记无需登录） ----------------

XHS_RE = re.compile(
    r"^https?://(?:[\w-]+\.)*(?:xhslink\.cn|xiaohongshu\.com)(?:/|$|\?)", re.I)


def _is_xhs(url: str) -> bool:
    return bool(XHS_RE.match(url))


def _pick_xhs_img(im: dict) -> str:
    """选画质最好的图片地址：优先无水印大图场景，其次详情页 1080 图"""
    infos = im.get("infoList") or []
    for scene in ("WB_PRV", "WB_DTL", "H5_DTL", "H5_PRV"):
        for i in infos:
            if (i.get("imageScene") or "") == scene and i.get("url"):
                return i["url"]
    return im.get("url") or im.get("urlDefault") or ""


def _parse_xhs_state(html: str):
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", html, re.S)
    if not m:
        return None
    try:
        # 页面里的 JS 对象字面量含 undefined，转成 null 后可按 JSON 解析
        return json.loads(m.group(1).replace("undefined", "null"))
    except Exception:
        return None


def _xhs_note_from_state(state: dict):
    """兼容新旧两种页面数据结构，取笔记正文"""
    nd = state.get("noteData") or {}
    note = ((nd.get("data") or {}).get("noteData")) or None
    if note:
        return note
    ndm = (state.get("note") or {}).get("noteDetailMap") or {}
    for v in ndm.values():
        n = (v or {}).get("note")
        if n:
            return n
    return None


XHS_RES_TIERS = [(4320, "8K"), (2160, "4K"), (1440, "2K"), (1080, "1080P"),
                 (720, "720P"), (480, "480P"), (360, "360P")]


def _xhs_streams(note: dict):
    """视频笔记的全部可下载流 [(height, url)]，按编码偏好排序（h264 兼容性最好）"""
    media = (note.get("video") or {}).get("media") or {}
    stream = media.get("videoStream") or media.get("stream") or {}
    out = []
    for key in ("h264", "avc", "av1", "h265", "h266"):
        for s in stream.get(key) or []:
            u = s.get("masterUrl") or ""
            if not u and s.get("backupUrls"):
                u = s.get("backupUrls")[0] or ""
            if not u:
                continue
            h = s.get("height") or s.get("width") or 0
            try:
                h = int(h)
            except Exception:
                h = 0
            out.append((h, u))
    return out


def _xhs_origin_url(note: dict) -> str:
    """
    无水印原片地址。页面里的转码流（masterUrl，MINI_APP 流）底部带
    「@作者 + 小红书」平台水印，而 consumer.originVideoKey 拼出的 CDN 直链
    是作者上传的原始文件——无水印、分辨率更高（实测 1088x1930 vs 720x1278）。
    """
    key = str(((note.get("video") or {}).get("consumer") or {}).get("originVideoKey")
              or "").strip()
    if not key:
        return ""
    return f"https://sns-video-bd.xhscdn.com/{key}"


def _xhs_origin_height(note: dict) -> int:
    """原片高度：优先 mediaV2（内嵌 JSON 字符串，含原始宽高），否则用转码流最大高度"""
    v = note.get("video") or {}
    mv = v.get("mediaV2")
    if isinstance(mv, str) and mv.strip():
        try:
            mv = json.loads(mv)
        except Exception:
            mv = None
    if isinstance(mv, dict):
        try:
            h = int((mv.get("video") or {}).get("height") or 0)
            if h:
                return h
        except Exception:
            pass
    hs = [h for h, _ in _xhs_streams(note) if h]
    return max(hs) if hs else 0


def _xhs_fallback_url(note: dict) -> str:
    """备用播放流：h264 转码流兼容性最好（原片可能是 HEVC，浏览器播不了时回退用）"""
    for key in ("h264", "avc", "av1", "h265", "h266"):
        for s in ((note.get("video") or {}).get("media") or {}).get("stream", {}).get(key) or []:
            u = s.get("masterUrl") or (s.get("backupUrls") or [""])[0] or ""
            if u:
                return u
    return ""


def _xhs_video_url(note: dict, height: int = 0) -> str:
    """
    播放/下载地址。优先无水印原片（originVideoKey）；
    原片缺失时回退转码流（height>0 时选不超过该高度的最高档）。
    注意：转码流全部带平台水印，只在原片拿不到时才用。
    """
    origin = _xhs_origin_url(note)
    if origin:
        return origin
    cands = _xhs_streams(note)
    if not cands:
        return ""
    if height:
        under = [c for c in cands if 0 < c[0] <= height]
        if under:
            cands = under
    return max(cands, key=lambda c: c[0])[1]


def _xhs_resolutions(note: dict):
    """把真实流高度映射成前端的档位标签（从低到高）。
    有无水印原片时只显示原片一档——转码流都带平台水印，不再作为可选画质。"""
    origin = _xhs_origin_url(note)
    if origin:
        h = _xhs_origin_height(note)
        if h:
            lab = next((lb for cap, lb in XHS_RES_TIERS if h >= cap), f"{h}P")
            return [lab]
    labels = []
    for h in sorted({h for h, _ in _xhs_streams(note) if h}):
        lab = next((lb for cap, lb in XHS_RES_TIERS if h >= cap), f"{h}P")
        if lab not in labels:
            labels.append(lab)
    return labels


def _xhs_fetch_note(url: str, proxy: str = "", cookie_browser: str = ""):
    """
    解析短链/长链 -> 页面 -> 笔记数据。返回 (final_url, note_dict)
    注意：小红书对桌面 UA 返回的是「需登录」空壳页，手机 UA 才会直出完整数据，
    因此按 手机UA -> 备用手机UA -> 桌面UA 依次尝试。
    """
    uas = [
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Mobile Safari/537.36",
        UA,
    ]
    opts = base_opts(proxy, "")
    opts["retries"] = 1
    opts["socket_timeout"] = 20
    last_err = None
    for ua in uas:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                resp = ydl.urlopen(sanitized_Request(url, headers={
                    "User-Agent": ua,
                    "Accept-Language": "zh-CN,zh;q=0.9",
                }))
                final = getattr(resp, "url", None)
                if not final and hasattr(resp, "geturl"):
                    final = resp.geturl()
                html = resp.read(8 << 20).decode("utf-8", errors="ignore")
            note = _xhs_note_from_state(_parse_xhs_state(html) or {})
            if note:
                return final or url, note
        except Exception as e:
            last_err = e
            continue
    if last_err and "ProxyError" in str(last_err):
        raise last_err
    raise RuntimeError("XHS_PARSE")


def _xhs_image_list(note: dict):
    """
    图文笔记的图片列表。
    优先用 fileId 拼原图地址（ci.xiaohongshu.com/{fileId}）——这是无水印的原始图片，
    分享页里的 webpic 地址带「小红书」角标水印且是压缩过的 1080 图。
    """
    imgs = []
    for im in note.get("imageList") or []:
        fid = im.get("fileId") or ""
        u = f"https://ci.xiaohongshu.com/{fid}" if fid else _pick_xhs_img(im)
        if u:
            imgs.append({"url": u, "ext": "jpg"})
    return imgs


def _xhs_cover(note: dict, images=None) -> str:
    """
    封面地址。新版页面 note.cover 只有 fileId（没有 url），
    需要用 ci.xiaohongshu.com/{fileId} 取原图；视频笔记再退回首帧图。
    """
    cv = note.get("cover")
    if isinstance(cv, dict):
        fid = cv.get("fileId") or ""
        if fid:
            return f"https://ci.xiaohongshu.com/{fid}"
        if cv.get("url"):
            return _original_image_url(cv["url"])
    elif isinstance(cv, str) and cv.startswith("http"):
        return _original_image_url(cv)
    for im in (images or note.get("imageList") or []):
        fid = im.get("fileId") or ""
        if fid:
            return f"https://ci.xiaohongshu.com/{fid}"
    frame = ((note.get("video") or {}).get("image") or {}).get("firstFrameFileid") or ""
    if frame:
        return f"https://ci.xiaohongshu.com/{frame}"
    return ""


def _xhs_cover_candidates(note: dict, images=None):
    """全部候选封面（大->小）：笔记封面 / 首帧 / 前几张图片，前端实测像素后取最大"""
    cands = [_xhs_cover(note, images)]
    frame = ((note.get("video") or {}).get("image") or {}).get("firstFrameFileid") or ""
    if frame:
        cands.append(f"https://ci.xiaohongshu.com/{frame}")
    for im in (images or note.get("imageList") or [])[:3]:
        fid = im.get("fileId") or ""
        u = im.get("url") or ""
        if fid:
            cands.append(f"https://ci.xiaohongshu.com/{fid}")
        elif u:
            cands.append(_original_image_url(u))
    return _dedup_urls(cands)[:6]


def xhs_analyze(url: str, proxy: str = "", cookie_browser: str = "") -> dict:
    """小红书笔记解析：图文 -> 图集；视频 -> mp4 直链"""
    final, note = _xhs_fetch_note(url, proxy, cookie_browser)

    # 笔记自带类型标记（video/normal），视频笔记即使带封面图也不能判成图集
    is_video_note = (note.get("type") == "video") or bool((note.get("video") or {}).get("media"))
    images = [] if is_video_note else _xhs_image_list(note)

    video_url = _xhs_video_url(note)
    # 标记为视频却取不到播放流（付费/受限内容），如实报错，不要拿封面冒充结果
    if is_video_note and not video_url:
        raise RuntimeError("xhs_parse 视频流地址缺失：该笔记可能是付费/受限内容，或页面结构已变更")
    if not is_video_note and not images:
        raise RuntimeError("xhs_parse 未取到任何图片：该笔记可能是付费/受限内容，或页面结构已变更")
    user = note.get("user") or {}
    title = (note.get("title") or note.get("desc") or "小红书笔记").strip() or "小红书笔记"
    # 描述里的 #话题# 标签去掉，避免文件名杂乱
    title = re.sub(r"#\S+?\[话题\]#", "", title).strip() or "小红书笔记"

    interact = note.get("interactInfo") or {}
    likes = interact.get("likedCount") or ""
    likes_n = int(likes) if str(likes).isdigit() else None

    return {
        "title": title,
        "author": user.get("nickName") or user.get("nickname") or "小红书用户",
        "cover": _xhs_cover(note, note.get("imageList")),
        "covers": _xhs_cover_candidates(note, note.get("imageList")),
        "duration": "--:--",
        "platform": "Xiaohongshu",
        "webpage": final,
        "resolutions": _xhs_resolutions(note) if is_video_note else [],
        "views": likes_n,
        "is_live": False,
        "content_type": "video" if is_video_note else ("image" if images else "audio"),
        "image_count": len(images),
        "images": images[:24],
        "ext": "jpg" if images else "mp4",
        "xhs": True,
        "xhs_video_url": video_url,
        "preview_url": video_url,
        # 原片可能是 HEVC 编码（部分浏览器不支持），预览播不了时退回 h264 转码流
        "preview_fallback_url": _xhs_fallback_url(note) if is_video_note else "",
        "preview_silent": False,
        "preview_audio_url": "",
    }


def _xhs_pack_images(task_id: str, url: str, proxy: str = "", cookie_browser: str = ""):
    """小红书图集下载：解析 -> 逐张下载 -> 多张打包 ZIP"""
    task = TASKS[task_id]
    final, note = _xhs_fetch_note(url, proxy, cookie_browser)
    images = _xhs_image_list(note)
    if not images:
        raise RuntimeError("XHS_PARSE")
    title = (note.get("title") or "小红书笔记").strip() or "小红书笔记"
    title = re.sub(r"#\S+?\[话题\]#", "", title).strip() or "小红书笔记"
    _pack_images(task_id, sanitize(title), images, final, proxy, cookie_browser)


def _xhs_download_video(task_id: str, url: str, kind: str, height: int = 0,
                        proxy: str = "", cookie_browser: str = ""):
    """小红书视频笔记下载：直链 mp4；kind=music 时再抽音轨为 m4a"""
    task = TASKS[task_id]
    final, note = _xhs_fetch_note(url, proxy, cookie_browser)
    video_url = _xhs_video_url(note, height)
    if not video_url:
        raise RuntimeError("XHS_PARSE")
    title = sanitize((note.get("title") or "小红书视频").strip() or "小红书视频")
    out_dir = title_out_dir(title, task_id)

    headers = {"User-Agent": UA, "Referer": "https://www.xiaohongshu.com/"}
    opts = base_opts(proxy, "")

    def _open(u):
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.urlopen(sanitized_Request(u, headers=headers)), u

    # 优先无水印原片；原片地址失效（如笔记刚改版/CDN 拒绝）时回退转码流
    try:
        resp, video_url = _open(video_url)
    except Exception:
        fb = _xhs_fallback_url(note)
        if not fb:
            raise
        resp, video_url = _open(fb)

    with resp:
        total = int(resp.headers.get("Content-Length") or 0)
        path = os.path.join(out_dir, f"{title}.mp4")
        done = 0
        with open(path, "wb") as fp:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                fp.write(chunk)
                done += len(chunk)
                if total:
                    task.update({"status": "downloading",
                                 "progress": int(done * 100 / total),
                                 "speed": "", "eta": 0})

    if kind in ("music", "audio"):
        task.update({"status": "processing", "progress": 95})
        if kind == "audio":
            audio_path = os.path.join(out_dir, f"{title}.mp3")
            cmd = [FFMPEG, "-y", "-i", path, "-vn", "-c:a", "libmp3lame", "-b:a", "192k", audio_path]
        else:
            audio_path = os.path.join(out_dir, f"{title}.m4a")
            cmd = [FFMPEG, "-y", "-i", path, "-vn", "-c:a", "copy", audio_path]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if os.path.exists(audio_path) and os.path.getsize(audio_path) > 1024:
            try:
                os.remove(path)
            except OSError:
                pass
            path = audio_path
        else:
            raise RuntimeError(f"音轨提取失败: {(r.stderr or '')[-200:]}")

    task.update({"status": "done", "progress": 100,
                 "filepath": path, "filename": os.path.basename(path)})


def run_xhs(task_id: str, url: str, kind: str, height: int = 0,
            proxy: str = "", cookie_browser: str = ""):
    """小红书任务入口：图集走打包，视频/音频走直链下载"""
    task = TASKS[task_id]
    try:
        if kind == "images":
            _xhs_pack_images(task_id, url, proxy, cookie_browser)
        else:
            _xhs_download_video(task_id, url, kind, height, proxy, cookie_browser)
    except Exception as e:
        code = classify_error(str(e))
        task.update({"status": "error",
                     "error": ZH_ERRORS.get(code, "下载失败"),
                     "detail": str(e)[:300],
                     "error_code": code})


# ---------------- 抖音专用解析（本机浏览器通道，免登录免签名） ----------------
# 2026-09 起抖音 Web 接口被 Argus 风控全面拦截（需 uifid/x-secsdk-web-signature），
# yt-dlp 与纯 HTTP 方案都会 403。唯一稳定做法（开源社区共识）是借真实浏览器环境
# 打开分享页，从页面自己发出的 detail 请求里拿无水印数据。
# 实现上复用本机已装的 Edge/Chrome（CDP 远程调试），无需额外下载浏览器。

DOUYIN_RE = re.compile(
    r"^https?://(?:[\w-]+\.)*(?:douyin\.com|iesdouyin\.com)(?:/|$|\?)", re.I)

_DY = {"proc": None, "port": 0, "profile": "", "lock": threading.Lock()}


def _is_douyin(url: str) -> bool:
    # qishui.douyin.com 是汽水音乐，走专用通道，不算抖音
    if _QISHUI_URL_RE.search(url or ""):
        return False
    return bool(DOUYIN_RE.match(url))


def _douyin_aweme_id(url: str) -> str:
    m = (re.search(r"/(?:video|note|share/(?:video|note))[/]?/(\d{6,})", url)
         or re.search(r"[?&]modal_id=(\d{6,})", url))
    return m.group(1) if m else ""


def _resolve_douyin_short(url: str) -> str:
    """v.douyin.com 短链 -> 302 后的真实地址（直连，不走代理）"""
    try:
        opener = urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            opener.open(req, timeout=12)
        except _RedirectCaught as e:
            return e.url or url
        except Exception as e:
            loc = (getattr(e, "headers", None) or {}).get("Location")
            if loc:
                return loc
    except Exception:
        pass
    return url


class _RedirectCaught(Exception):
    def __init__(self, url):
        super().__init__(url)
        self.url = url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _RedirectCaught(newurl)


def _douyin_short_via_cdp(url: str, timeout_s: int = 16) -> str:
    """
    v.douyin.com 短链的 HTTP 302 现在只回主页（拿不到作品 ID）。
    改用 CDP 浏览器真实打开短链，轮询最终落地页地址，返回带 /video/{id} 的 URL。
    """
    port = _dy_cdp_browser()
    target = None
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/json/new?url=about:blank",
            method="PUT", headers={"User-Agent": UA})
        try:
            t = json.loads(urllib.request.urlopen(req, timeout=5).read())
        except Exception:
            t = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/new?url=about:blank", timeout=5).read())
        target = t.get("id") or ""
        ws_url = t.get("webSocketDebuggerUrl") or ""
        if not (target and ws_url):
            raise RuntimeError("DOUYIN_TAB_FAIL")
        ws = websocket.create_connection(ws_url, timeout=15)
        mid = [0]

        def send(method, params=None):
            mid[0] += 1
            ws.send(json.dumps({"id": mid[0], "method": method,
                                "params": params or {}}))
            return mid[0]

        send("Page.enable")
        send("Page.navigate", {"url": url})
        final = url
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            time.sleep(0.8)
            r = send("Runtime.evaluate",
                     {"expression": "location.href", "returnByValue": True})
            while True:
                try:
                    m = json.loads(ws.recv())
                except Exception:
                    continue
                if m.get("id") == r:
                    v = ((m.get("result") or {}).get("result") or {}).get("value") or ""
                    if v and v != final:
                        final = v
                        if _douyin_aweme_id(v):
                            return v
                    break
        return final
    finally:
        if target:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{target}",
                                       timeout=3).read()
            except Exception:
                pass


def _douyin_resolve(url: str):
    """返回 (aweme_id, webpage)。短链先跟随跳转再取 ID，取不到时走浏览器兜底"""
    aid = _douyin_aweme_id(url)
    webpage = url
    if not aid:
        webpage = _resolve_douyin_short(url)
        aid = _douyin_aweme_id(webpage)
    if not aid and re.match(r"^https?://v\.douyin\.com/", url, re.I):
        webpage = _douyin_short_via_cdp(url)
        aid = _douyin_aweme_id(webpage)
    return aid, webpage


def _dy_cdp_browser():
    """懒启动一个无痕的 headless Edge/Chrome 并保活，返回 (port, proc)；已启动则复用"""
    with _DY["lock"]:
        if _DY["proc"] is not None and _DY["proc"].poll() is None:
            return _DY["port"]
        if websocket is None:
            raise RuntimeError("DOUYIN_NO_WS: 缺少 websocket-client 库（pip install websocket-client）")
        exe = ""
        for p in (os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
                  os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
                  os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                  os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                  os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe")):
            if os.path.exists(p):
                exe = p
                break
        if not exe:
            for name in ("msedge", "chrome", "chromium"):
                w = shutil.which(name)
                if w:
                    exe = w
                    break
        if not exe:
            raise RuntimeError("DOUYIN_NO_BROWSER: 本机未找到 Edge/Chrome，无法解析抖音")
        port = random.randint(21000, 29999)
        profile = tempfile.mkdtemp(prefix="dy_cdp_")
        proc = subprocess.Popen(
            [exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
             "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check", "--mute-audio", "--window-size=1280,900",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok = False
        for _ in range(80):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1).read()
                ok = True
                break
            except Exception:
                time.sleep(0.25)
        if not ok:
            try:
                proc.terminate()
            except Exception:
                pass
            shutil.rmtree(profile, ignore_errors=True)
            raise RuntimeError("DOUYIN_BROWSER_FAIL: 浏览器调试端口启动失败")
        _DY.update(proc=proc, port=port, profile=profile)
        atexit.register(_dy_cdp_close)
        return port


def _dy_cdp_close():
    try:
        if _DY.get("proc"):
            _DY["proc"].terminate()
    except Exception:
        pass
    try:
        shutil.rmtree(_DY.get("profile") or "", ignore_errors=True)
    except Exception:
        pass
    _DY.update(proc=None, port=0, profile="")


def _douyin_fetch_detail(aweme_id: str, timeout_s: int = 35) -> dict:
    """
    用 CDP 打开视频页，等页面自己发出 detail 请求（浏览器环境可过 Argus 风控），
    抓响应体返回 aweme_detail。全程不登录、不执行任何验证码。
    """
    port = _dy_cdp_browser()
    target = None
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?url=about:blank",
                                     method="PUT", headers={"User-Agent": UA})
        try:
            t = json.loads(urllib.request.urlopen(req, timeout=5).read())
        except Exception:
            t = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/new?url=about:blank", timeout=5).read())
        target = t.get("id") or ""
        ws_url = t.get("webSocketDebuggerUrl") or ""
        if not (target and ws_url):
            raise RuntimeError("DOUYIN_TAB_FAIL")
        ws = websocket.create_connection(ws_url, timeout=15)
        mid = [0]

        def send(method, params=None):
            mid[0] += 1
            ws.send(json.dumps({"id": mid[0], "method": method,
                                "params": params or {}}))
            return mid[0]

        send("Network.enable")
        send("Page.enable")
        send("Page.navigate", {"url": f"https://www.douyin.com/video/{aweme_id}"})

        bodies = []          # [(url, body)]
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            try:
                msg = json.loads(ws.recv())
            except Exception:
                continue
            if msg.get("method") != "Network.responseReceived":
                continue
            p = msg["params"]
            u = (p.get("response") or {}).get("url") or ""
            if "/aweme/v1/web/aweme/detail/" not in u:
                continue
            rid = p.get("requestId")
            r2 = send("Network.getResponseBody", {"requestId": rid})
            t1 = time.time()
            while time.time() - t1 < 4:
                try:
                    m2 = json.loads(ws.recv())
                except Exception:
                    break
                if m2.get("id") == r2:
                    b = (m2.get("result") or {}).get("body") or ""
                    if b:
                        bodies.append((u, b))
                        try:
                            o = json.loads(b)
                            det = o.get("aweme_detail") or {}
                            if str(det.get("aweme_id") or "") == aweme_id:
                                return det
                        except Exception:
                            pass
                    break
        raise RuntimeError("DOUYIN_CDP_TIMEOUT: 未能从页面取到视频数据（页面可能需要人工验证）")
    finally:
        if target:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{target}",
                                       timeout=3).read()
            except Exception:
                pass


def _dy_stream_hw(addr: dict, gear_name: str = "") -> int:
    """推断一条流的真实高度：优先 play_addr 自带的 height，其次 gear_name 里的数字"""
    h = int(addr.get("height") or 0)
    if h:
        return h
    m = re.search(r"(\d{3,4})", str(gear_name or ""))
    return int(m.group(1)) if m else 0


def _dy_candidates(detail: dict):
    """汇总所有可用无水印流 [(height, bitrate, url)]，含 bit_rate 各档 + h264/h265 默认流"""
    v = detail.get("video") or {}
    out, seen = [], set()

    def add(addr, br=0, gear=""):
        urls = (addr or {}).get("url_list") or []
        if not urls:
            return
        u = urls[0]
        if u in seen:
            return
        seen.add(u)
        out.append((_dy_stream_hw(addr or {}, gear), int(br or 0), u))

    for b in v.get("bit_rate") or []:
        add(b.get("play_addr") or {}, b.get("bit_rate") or 0, b.get("gear_name") or "")
    for key in ("play_addr", "play_addr_h264", "play_addr_265"):
        add(v.get(key) or {}, 0, "")
    if not out:
        add(v.get("download_addr") or {})
    return out


def _dy_pick_video(detail: dict, height: int = 0) -> str:
    """选最高画质的无水印直链：先按分辨率，再按码率（同分辨率优先 h264 兼容性）"""
    cands = _dy_candidates(detail)
    if not cands:
        return ""
    if height:
        under = [c for c in cands if 0 < c[0] <= height]
        if under:
            cands = under
    cands.sort(key=lambda c: (c[0], c[1]))
    return cands[-1][2]


def _dy_resolutions(detail: dict):
    """档位标签（低→高）：来自每条流的真实分辨率；达到源视频高度的一档标为「原画」"""
    v = detail.get("video") or {}
    src_h = int(v.get("height") or 0)
    hs = sorted({h for h, _, _ in _dy_candidates(detail) if h})
    labels = []
    for h in hs:
        if src_h and h == src_h:
            lab = "原画"
        else:
            lab = next((lb for cap, lb in RES_TIERS if h >= cap), f"{h}P")
        if lab not in labels:
            labels.append(lab)
    return labels


def douyin_analyze(url: str, proxy: str = "", cookie_browser: str = "") -> dict:
    """抖音笔记解析：视频/图集 -> 无水印直链（借助本机浏览器，免登录）"""
    aid, webpage = _douyin_resolve(url)
    if not aid:
        raise RuntimeError("DOUYIN_PARSE: 链接里没有找到作品 ID")
    detail = _douyin_fetch_detail(aid)
    if not detail:
        raise RuntimeError("DOUYIN_PARSE")

    user = detail.get("author") or {}
    title = (detail.get("desc") or "抖音视频").strip() or "抖音视频"
    title = re.sub(r"#\S+", "", title).strip() or "抖音视频"

    images = []
    for im in detail.get("images") or []:
        urls = im.get("url_list") or []
        if urls:
            images.append({"url": _original_image_url(urls[0]), "ext": "jpg"})

    v = detail.get("video") or {}
    video_url = _dy_pick_video(detail)          # 下载：最高画质（可能是 HEVC 4K）
    # 预览：优先 H.264 流，浏览器原生能播（HEVC 多数浏览器播不了画面）
    preview_url = (((v.get("play_addr_h264") or {}).get("url_list")
                    or (v.get("play_addr") or {}).get("url_list") or [""])[0]) or video_url
    cover_urls = _dedup_urls(
        [u for lst in (v.get("origin_cover"), v.get("cover"), v.get("dynamic_cover"))
         for u in ((lst or {}).get("url_list") or [])])
    covers = [_original_image_url(u) for u in cover_urls][:6]
    dur = int(v.get("duration") or 0)
    m, s = divmod(max(dur, 0) // 1000, 60)

    is_video = bool(video_url) and not images
    return {
        "title": title,
        "author": user.get("nickname") or "抖音用户",
        "cover": covers[0] if covers else "",
        "covers": covers,
        "duration": f"{m}:{s:02d}",
        "platform": "Douyin",
        "webpage": webpage,
        "resolutions": _dy_resolutions(detail) if is_video else [],
        "views": detail.get("statistics", {}).get("digg_count"),
        "is_live": False,
        "content_type": "video" if is_video else ("image" if images else "audio"),
        "image_count": len(images),
        "images": images[:30],
        "ext": "jpg" if images else "mp4",
        "douyin": True,
        "douyin_video_url": video_url,
        "preview_url": preview_url,
        "preview_silent": False,
        "preview_audio_url": "",
    }


def _douyin_download(task_id: str, url: str, kind: str, height: int = 0,
                     proxy: str = "", cookie_browser: str = ""):
    """抖音下载：视频直链（无水印）/ 图集打包 / 音乐抽音轨"""
    task = TASKS[task_id]
    aid, _ = _douyin_resolve(url)
    if not aid:
        raise RuntimeError("DOUYIN_PARSE")
    detail = _douyin_fetch_detail(aid)
    if not detail:
        raise RuntimeError("DOUYIN_PARSE")
    title = sanitize((detail.get("desc") or "抖音视频").strip() or "抖音视频")
    title = re.sub(r"#\S+", "", title).strip() or "抖音视频"
    out_dir = title_out_dir(title, task_id)

    if kind == "images":
        images = []
        for im in detail.get("images") or []:
            urls = im.get("url_list") or []
            if urls:
                images.append({"url": _original_image_url(urls[0]), "ext": "jpg"})
        if not images:
            raise RuntimeError("未在该链接中找到图片（这可能是一条视频）")
        _pack_images(task_id, title, images, f"https://www.douyin.com/video/{aid}",
                     proxy, cookie_browser)
        return

    if kind in ("audio", "music"):
        urls = ((detail.get("music") or {}).get("play_url") or {}).get("url_list") or []
        if not urls:
            raise RuntimeError("这条内容没有可提取的音乐")
        mu = urls[0]
        headers = {"User-Agent": UA, "Referer": "https://www.douyin.com/"}
        raw = os.path.join(out_dir, f"{title}.m4a")
        _download_url_to(mu, raw, headers, task)
        if kind == "audio":
            path = os.path.join(out_dir, f"{title}.mp3")
            task.update({"status": "processing", "progress": 95})
            r = subprocess.run([FFMPEG, "-y", "-i", raw, "-vn", "-c:a", "libmp3lame",
                                "-b:a", "192k", path],
                               capture_output=True, text=True, timeout=300)
            if os.path.exists(path) and os.path.getsize(path) > 1024:
                try:
                    os.remove(raw)
                except OSError:
                    pass
            else:
                raise RuntimeError(f"音轨转换失败: {(r.stderr or '')[-200:]}")
        else:
            path = raw
        task.update({"status": "done", "progress": 100,
                     "filepath": path, "filename": os.path.basename(path)})
        return

    video_url = _dy_pick_video(detail, height)
    if not video_url:
        raise RuntimeError("DOUYIN_PARSE")
    path = os.path.join(out_dir, f"{title}.mp4")
    headers = {"User-Agent": UA, "Referer": "https://www.douyin.com/"}
    _download_url_to(video_url, path, headers, task)
    task.update({"status": "done", "progress": 100,
                 "filepath": path, "filename": os.path.basename(path)})


def _download_url_to(u: str, path: str, headers: dict, task: dict):
    """直链下载 + 进度上报（对带签名时效的平台直链统一用这个）"""
    opts = base_opts("", "")
    with yt_dlp.YoutubeDL(opts) as ydl:
        resp = ydl.urlopen(sanitized_Request(u, headers=headers))
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(path, "wb") as fp:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                fp.write(chunk)
                done += len(chunk)
                if total:
                    task.update({"status": "downloading",
                                 "progress": int(done * 100 / total),
                                 "speed": "", "eta": 0})


def run_douyin(task_id: str, url: str, kind: str, height: int = 0,
               proxy: str = "", cookie_browser: str = ""):
    task = TASKS[task_id]
    try:
        _douyin_download(task_id, url, kind, height, proxy, cookie_browser)
    except Exception as e:
        code = classify_error(str(e))
        task.update({"status": "error",
                     "error": ZH_ERRORS.get(code, "下载失败"),
                     "detail": str(e)[:300],
                     "error_code": code})



@app.route("/")
def index():
    return render_template("index.html")


@app.route("/vendor/hls.light.min.js")
def vendor_hls():
    """hls.js（Apache-2.0，已内置 assets/）：m3u8 分片流的浏览器在线预览"""
    p = os.path.join(BASE_DIR, "assets", "hls.light.min.js")
    if not os.path.exists(p):
        return Response("", status=404)
    return send_file(p, mimetype="application/javascript")


# Local tool, but the page may be reached through "localhost" while the server
# binds 127.0.0.1; allow loopback origins so those requests are never blocked,
# and make sure the browser never serves a stale page from cache.
LOOPBACK_ORIGIN = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$")


@app.after_request
def _local_headers(resp):
    origin = request.headers.get("Origin", "")
    if origin and LOOPBACK_ORIGIN.match(origin):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Vary"] = "Origin"
    if request.path == "/" or request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/api/ping")
def api_ping():
    """Tiny liveness probe used by the UI status banner."""
    return jsonify({"ok": True, "app": "ume", "ts": int(time.time())})


@app.route("/api/sph_status")
def api_sph_status():
    cookie = _sph_cookie()
    return jsonify({"ok": True, "has_cookie": bool(cookie),
                    "hint": cookie[:3] + "…" if cookie else ""})


@app.route("/api/sph_cookie", methods=["POST"])
def api_sph_cookie():
    """保存 / 清除腾讯元宝 Cookie（视频号解析用）"""
    data = request.get_json(silent=True) or {}
    try:
        _sph_save_cookie(data.get("cookie") or "")
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    return jsonify({"ok": True, "has_cookie": bool(_sph_cookie())})


def _sph_dpapi_unprotect(data: bytes) -> bytes:
    """Windows DPAPI 解密（Chrome/Edge Local State 里的 AES 密钥用 DPAPI 保护）"""
    import ctypes
    from ctypes import wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.c_void_p)]

    k32, crypt32 = ctypes.windll.kernel32, ctypes.windll.crypt32
    k32.LocalFree.argtypes = [wintypes.HLOCAL]
    buf = ctypes.create_string_buffer(data, len(data))
    bin_ = _BLOB(len(data), ctypes.cast(buf, ctypes.c_void_p))
    bout = _BLOB()
    if crypt32.CryptUnprotectData(
            ctypes.byref(bin_), None, None, None, None, 0, ctypes.byref(bout)) == 0:
        raise OSError("DPAPI decrypt failed")
    try:
        return ctypes.string_at(bout.pbData, bout.cbData)
    finally:
        k32.LocalFree(bout.pbData)


def _sph_aes_gcm_decrypt(key: bytes, encrypted: bytes) -> str:
    """解 Chrome/Edge v10/v11 Cookie：前 3 字节版本号 + 12 字节 nonce + 密文(尾部 16 字节 tag)"""
    from Cryptodome.Cipher import AES
    nonce, cipher = encrypted[3:15], encrypted[15:]
    plain = AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt(cipher)[:-16]
    return plain.decode("utf-8", "replace")


def _sph_open_sqlite_ro(path: str):
    """以 immutable 只读模式打开 SQLite——浏览器运行中文件被锁也能读"""
    return sqlite3.connect(pathlib.Path(path).resolve().as_uri() + "?immutable=1", uri=True)


def _sph_chromium_key(user_data: str) -> bytes:
    """从 Local State 解出 AES 主密钥"""
    with open(os.path.join(user_data, "Local State"), encoding="utf-8") as fp:
        enc = base64.b64decode(json.load(fp)["os_crypt"]["encrypted_key"])
    key = _sph_dpapi_unprotect(enc[5:])          # 去掉 b"DPAPI" 前缀
    if not key:
        raise OSError("empty AES key")
    return key


def _sph_chromium_cookie(browser: str) -> tuple:
    """
    直接读 Chromium 系浏览器（edge/chrome）的 Cookie 库，浏览器运行中也可用：
    immutable 只读连接绕开文件锁，AES-GCM 解 v10/v11，v20(app-bound) 无法解则单独计数。
    返回 (cookie 字符串, app_bound 数量)
    """
    if sys.platform != "win32":
        raise RuntimeError("windows only")
    local = os.environ.get("LOCALAPPDATA") or ""
    if browser == "edge":
        user_data = os.path.join(local, "Microsoft", "Edge", "User Data")
    elif browser == "chrome":
        user_data = os.path.join(local, "Google", "Chrome", "User Data")
    else:
        raise ValueError(browser)
    if not os.path.isdir(user_data):
        raise FileNotFoundError(f"本机没有安装 {browser}（找不到 {user_data}）")

    key = _sph_chromium_key(user_data)
    profiles = ["Default"] + sorted(
        d for d in os.listdir(user_data) if re.fullmatch(r"Profile \d+", d))
    pairs, app_bound = {}, 0
    for prof in profiles:
        db = os.path.join(user_data, prof, "Network", "Cookies")
        if not os.path.isfile(db):
            db = os.path.join(user_data, prof, "Cookies")
        if not os.path.isfile(db):
            continue
        try:
            con = _sph_open_sqlite_ro(db)
        except (sqlite3.Error, PermissionError) as e:
            raise SphError(_sph_classify_db_error(e), f"{db}: {e}")
        try:
            for host, name, enc in con.execute(
                    "SELECT host_key, name, encrypted_value FROM cookies"):
                dom = (host or "").lstrip(".")
                if "tencent.com" not in dom:
                    continue
                if not enc:
                    continue
                if enc[:3] in (b"v10", b"v11"):
                    try:
                        pairs[f"{name}|{dom}"] = f"{name}={_sph_aes_gcm_decrypt(key, enc)}"
                    except Exception:
                        pass
                elif enc[:3] == b"v20":
                    app_bound += 1
        finally:
            con.close()
    cookie = "; ".join(pairs.values())
    return cookie, app_bound


def _sph_classify_db_error(e: Exception) -> str:
    """Cookie 库打不开：是「被浏览器锁住」还是「读不了」"""
    s = str(e).lower()
    if "unable to open" in s or "locked" in s or "permission" in s:
        return "SPH_COOKIE_LOCK"
    return "SPH_COOKIE_READ"


def _sph_firefox_cookie() -> str:
    """读 Firefox cookies.sqlite（明文存储），浏览器运行中也可用"""
    appdata = os.environ.get("APPDATA") or ""
    root = os.path.join(appdata, "Mozilla", "Firefox", "Profiles")
    if not os.path.isdir(root):
        raise FileNotFoundError("本机没有安装 Firefox")
    pairs = {}
    for prof in sorted(os.listdir(root)):
        db = os.path.join(root, prof, "cookies.sqlite")
        if not os.path.isfile(db):
            continue
        try:
            con = _sph_open_sqlite_ro(db)
        except (sqlite3.Error, PermissionError) as e:
            raise SphError(_sph_classify_db_error(e), f"{db}: {e}")
        try:
            for host, name, value in con.execute(
                    "SELECT host, name, value FROM moz_cookies"):
                if "tencent.com" not in (host or "").lstrip(".") or not value:
                    continue
                pairs[f"{name}|{host}"] = f"{name}={value}"
        finally:
            con.close()
    return "; ".join(pairs.values())


# Cookie 读取错误文案：服务端按界面语言直接返回，前端只管显示
# （避免依赖前端脚本版本——旧页面也能显示正确语言）
_SPHC_MSGS = {
    "COOKIE_LOCK": {
        "zh": "这个浏览器还在后台运行（关掉窗口不代表退出了——Windows 的「启动加速」"
              "会保留后台进程，把 Cookie 库整个锁住）。请：任务栏右键浏览器图标 → 退出；"
              "或在任务管理器结束所有 msedge.exe / chrome.exe 进程后再点",
        "en": "This browser is still running in the background (closing the window does not "
              "quit it - Windows 'Startup boost' keeps background processes, which lock the "
              "cookie database). Quit it from the taskbar icon or end all msedge.exe / "
              "chrome.exe processes in Task Manager, then click again",
    },
    "APP_BOUND": {
        "zh": "这个浏览器的 Cookie 用了新版加密（app-bound），程序读不出来。"
              "请按 F12 → Network → 刷新页面 → 点任意请求，把请求头里 Cookie: 后面整串复制到输入框保存",
        "en": "This browser encrypts its cookies with the new app-bound scheme, which "
              "cannot be read by apps. Press F12 -> Network -> reload the page -> click any "
              "request, then copy the whole Cookie header into the box and save",
    },
    "NO_LOGIN": {
        "zh": "该浏览器里没有腾讯元宝的登录态，请先在这个浏览器打开 yuanbao.tencent.com 登录一次再点",
        "en": "No Yuanbao sign-in found in this browser. Open yuanbao.tencent.com in this "
              "browser, sign in once, then click again",
    },
    "NOT_INSTALLED": {
        "zh": "本机没有安装这个浏览器。请改用你已登录 yuanbao.tencent.com 的那个浏览器，"
              "或用 F12 手动复制 Cookie",
        "en": "This browser is not installed. Use the browser where you signed in to "
              "yuanbao.tencent.com, or copy the cookie manually via F12",
    },
    "READ_FAIL": {
        "zh": "读取浏览器 Cookie 失败",
        "en": "Failed to read the browser's cookies",
    },
}


def _sphc_msg(code: str, lang: str) -> str:
    d = _SPHC_MSGS.get(code) or {}
    return d.get(lang) or d.get("zh") or code


@app.route("/api/sph_cookie_from_browser", methods=["POST"])
def api_sph_cookie_from_browser():
    """从本机浏览器读腾讯元宝登录态（浏览器需已登录 yuanbao.tencent.com；运行中也能读）

    错误文案由服务端按 lang 直接本地化（code 同时返回，供前端兜底）。
    """
    data = request.get_json(silent=True) or {}
    browser = (data.get("browser") or "edge").lower()
    lang = "en" if (data.get("lang") or "").lower().startswith("en") else "zh"
    try:
        app_bound = 0
        try:
            if browser == "firefox":
                cookie = _sph_firefox_cookie()
            else:
                cookie, app_bound = _sph_chromium_cookie(browser)
        except SphError as e:
            if e.code == "SPH_COOKIE_LOCK":
                return jsonify({"ok": False, "code": "COOKIE_LOCK",
                                "error": _sphc_msg("COOKIE_LOCK", lang)}), 400
            return jsonify({"ok": False, "code": "COOKIE_READ",
                            "error": ("Cannot read this browser's cookie database: "
                                      if lang == "en" else "读不了 Cookie 库：") +
                                     str(e.detail)[:160]}), 400
        if not cookie:
            if app_bound:
                return jsonify({"ok": False, "code": "APP_BOUND",
                                "error": _sphc_msg("APP_BOUND", lang)}), 400
            return jsonify({"ok": False, "code": "NO_LOGIN",
                            "error": _sphc_msg("NO_LOGIN", lang)}), 400
        _sph_save_cookie(cookie)
        return jsonify({"ok": True, "count": len(cookie.split("; "))})
    except FileNotFoundError as e:
        return jsonify({"ok": False, "code": "NOT_INSTALLED", "browser": browser,
                        "error": _sphc_msg("NOT_INSTALLED", lang)}), 400
    except Exception as e:
        return jsonify({"ok": False, "code": "READ_FAIL",
                        "error": _sphc_msg("READ_FAIL", lang) + ("" if lang == "en" else "：")
                                 + str(e)[:160],
                        "detail": str(e)[:200]}), 400


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    data = request.get_json(silent=True) or {}
    url = extract_url(data.get("url", ""))
    if not url:
        return jsonify({"error": "没有识别到链接：请粘贴包含网址的完整分享文案"
                                 "（如 v.douyin.com/xxxx 或 https://… 开头的链接），只有文字是不够的",
                        "code": "EMPTY_URL"}), 400
    if not re.match(r"^https?://", url):
        url = "https://" + url
    proxy = data.get("proxy") or ""
    cookie_browser = data.get("cookie_browser") or ""
    if SPH_RE.match(url):
        try:
            return jsonify(analyze_sph(url, proxy))
        except SphError as e:
            return jsonify({"error": e.detail or e.code, "detail": e.detail,
                            "code": e.code}), 400
        except Exception as e:
            return jsonify({"error": str(e)[:200], "detail": str(e)[:300],
                            "code": "SPH_PARSE"}), 400
    if _is_xhs(url):
        try:
            return jsonify(xhs_analyze(url, proxy, cookie_browser))
        except Exception as e:
            code = classify_error(str(e))
            return jsonify({"error": ZH_ERRORS.get(code, "解析失败"),
                            "detail": str(e)[:300], "code": code}), 400
    # 抖音：Argus 风控已封锁纯 HTTP 方案，直接走本机浏览器通道（免登录、无水印）
    if _is_douyin(url):
        try:
            return jsonify(douyin_analyze(url, proxy, cookie_browser))
        except Exception as e:
            code = classify_error(str(e))
            return jsonify({"error": ZH_ERRORS.get(code, "解析失败"),
                            "detail": str(e)[:300], "code": code}), 400
    # X/推特：先试图集镜像通道（不依赖 x.com 直连，图集帖命中就直接返回，避免长时间卡住）
    if TW_URL_RE.match(url):
        try:
            tw = _twitter_fetch(url, proxy, cookie_browser)
        except Exception:
            tw = None
        if tw:
            return _twitter_gallery_response(tw, proxy, cookie_browser)
    # 漫画站：章节阅读页 -> 整话页图（图集形态返回，下载自动打包「XXX漫画-XXX话.zip」）
    if _is_manga(url):
        try:
            return jsonify(_manga_analyze(url))
        except Exception as e:
            code = classify_error(str(e))
            return jsonify({"error": ZH_ERRORS.get(code, "解析失败"),
                            "detail": str(e)[:300], "code": code}), 400
    try:
        return jsonify(analyze_url(url, proxy, cookie_browser))
    except Exception as e:
        code = classify_error(str(e))
        # X/推特：yt-dlp 失败（误报无视频、SSL 被掐、超时等）都再试一次镜像图集通道
        if TW_URL_RE.match(url):
            try:
                tw = _twitter_fetch(url, proxy, cookie_browser)
            except Exception as e2:
                c2 = classify_error(str(e2))
                return jsonify({"error": ZH_ERRORS.get(c2, "解析失败"),
                                "detail": str(e2)[:300], "code": c2}), 400
            if tw:
                return _twitter_gallery_response(tw, proxy, cookie_browser)
            # 视频推文：yt-dlp 对未登录访客常误报 unavailable/要求登录，
            # 镜像多半能直接拿到视频直链，不再把用户推去折腾登录
            try:
                tv = _twitter_video_fetch(url, proxy, cookie_browser)
            except Exception:
                tv = None
            if tv:
                return _twitter_video_response(tv)
        return jsonify({"error": ZH_ERRORS.get(code, "解析失败"),
                        "detail": str(e)[:300], "code": code}), 400


def _target_from_url(url: str) -> str:
    """从用户粘贴的链接提取站点首页，作为连通性测试目标"""
    m = re.match(r"^(https?://[^/]+)", url or "")
    return m.group(1) + "/" if m else "https://www.youtube.com/generate_204"


def _check_target(proxy: str, target: str = "https://www.youtube.com/generate_204",
                  timeout_s: int = 6):
    """检测某个代理（或直连）能否访问目标站点，返回 (proxy, ok)
    只要目标返回任意 HTTP 状态码（含 403/302 等）即视为可达，000/超时为不可达"""
    cmd = ["curl", "-s", "-o", os.devnull, "-w", "%{http_code}",
           "--max-time", str(timeout_s),
           "-A", UA]
    if proxy:
        cmd += ["-x", proxy]          # 指定代理时覆盖环境变量
    else:
        cmd += ["--noproxy", "*"]     # 直连时忽略环境变量代理
    cmd.append(target)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s + 5).stdout.strip()
        return proxy, out not in ("", "000")
    except Exception:
        return proxy, False


@app.route("/api/test_proxy", methods=["POST"])
def api_test_proxy():
    """手动测试用户填写的代理能否访问目标链接的平台"""
    data = request.get_json(silent=True) or {}
    proxy = data.get("proxy", "")
    target = _target_from_url(data.get("url") or "")
    t0 = time.time()
    _, ok = _check_target(proxy, target, timeout_s=12)
    ms = int((time.time() - t0) * 1000)
    if ok:
        return jsonify({"ok": True, "code": "OK", "ms": ms, "target": target})
    return jsonify({"ok": False, "target": target,
                    "error": f"无法通过该地址访问 {target} ，请确认代理软件已开启且端口正确"})


# 常见代理软件的本地 HTTP 端口（Clash Verge / Clash / v2rayN / Shadowrocket / Privoxy 等）
AUTO_DETECT_PORTS = [7897, 7890, 7899, 7891, 10809, 10808, 8118, 20171, 8080]


def _local_proxy_candidates() -> list:
    """收集本机可能的代理地址（按优先级）：
    ① 环境变量代理 ② Windows 系统代理（注册表）③ 正在监听的本地回环端口
    （Clash Verge 等允许随机端口，扫描实际监听端口才能兜住）④ 常见端口名单"""
    cands = []

    def add(p):
        p = (p or "").rstrip("/")
        if p and p not in cands:
            cands.append(p)

    # ① 环境变量
    add(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "")

    # ② Windows 系统代理（开了「系统代理」开关的软件都会写这里）
    if os.name == "nt":
        try:
            import winreg
            k = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
            try:
                enable, _ = winreg.QueryValueEx(k, "ProxyEnable")
                if enable:
                    server, _ = winreg.QueryValueEx(k, "ProxyServer")
                    # 可能是 "127.0.0.1:7897" 或 "http=...;https=..." 形式
                    if "=" in server:
                        for part in server.split(";"):
                            if part.lower().startswith(("http=", "https=")):
                                server = part.split("=", 1)[1]
                                break
                    server = (server or "").strip()
                    if server and "://" not in server:
                        server = "http://" + server
                    add(server)
            finally:
                winreg.CloseKey(k)
        except Exception:
            pass

    # ③ 本机正在监听的回环端口（netstat 扫描，自动覆盖随机端口）
    own_port = int(os.environ.get("PORT", "8765"))
    skip_ports = {own_port, 19000, 9222}   # 自身服务 / 常见调试端口
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, timeout=10,
                             encoding="mbcs" if os.name == "nt" else "utf-8",
                             errors="replace").stdout
        loopback, wildcard = set(), set()
        for line in out.splitlines():
            if "LISTENING" not in line.upper():
                continue
            parts = line.split()
            if len(parts) >= 2 and ":" in parts[1]:
                addr = parts[1].rsplit(":", 1)
                try:
                    p = int(addr[1])
                except ValueError:
                    continue
                if p in skip_ports or p < 1024:
                    continue
                if addr[0] == "127.0.0.1":
                    loopback.add(p)
                elif addr[0] == "0.0.0.0":
                    wildcard.add(p)
        # 只绑回环的端口更可能是本地代理，排前面
        for p in sorted(loopback) + sorted(wildcard - loopback):
            add(f"http://127.0.0.1:{p}")
    except Exception:
        pass

    # ④ 常见端口名单兜底（netstat 失败时也有基本覆盖）
    for p in AUTO_DETECT_PORTS:
        add(f"http://127.0.0.1:{p}")
    return cands[:64]      # 上限保护：太多候选会拖慢并行探测


@app.route("/api/auto_detect", methods=["POST"])
def api_auto_detect():
    """并行探测本机代理（环境变量 / 系统代理 / 监听端口 / 常见端口）+ 直连，
    以用户粘贴的链接所属平台为目标，返回第一个可访问该平台的方案"""
    data = request.get_json(silent=True) or {}
    target = _target_from_url(data.get("url") or "")

    candidates = _local_proxy_candidates()
    candidates.append("")  # 直连兜底

    with ThreadPoolExecutor(max_workers=len(candidates)) as ex:
        results = dict(ex.map(lambda p: _check_target(p, target, 8), candidates))

    # 优先直连（更快、不依赖代理），其次按候选顺序（常见程度）
    if results.get(""):
        return jsonify({"ok": True, "proxy": "", "direct": True, "target": target})
    for c in candidates[:-1]:
        if results.get(c):
            return jsonify({"ok": True, "proxy": c, "direct": False, "target": target})
    return jsonify({"ok": False, "target": target,
                    "error": "未检测到任何可访问该平台的网络方案（直连和代理都失败）"})


def _find_browser_exe(browser: str) -> str:
    """定位浏览器可执行文件路径"""
    candidates = {
        "edge": [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
                 r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"],
        "chrome": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                   r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                   r"%LocalAppData%\Google\Chrome\Application\chrome.exe"],
    }.get(browser, [])
    for c in candidates:
        p = os.path.expandvars(c)
        if os.path.exists(p):
            return p
    return ""


@app.route("/api/export_cookies_auto", methods=["POST"])
def api_export_cookies_auto():
    """
    一键导出 Cookie：自动关闭浏览器 -> 导出 -> 自动重启浏览器（恢复标签页）
    用户只需在页面上确认一次。
    """
    browser = (request.get_json(silent=True) or {}).get("browser", "")
    if browser not in ("edge", "chrome"):
        return jsonify({"ok": False, "error": "该浏览器不支持一键导出，请选择 Edge 或 Chrome"})

    proc_name = "msedge.exe" if browser == "edge" else "chrome.exe"
    # 1. 关闭浏览器
    subprocess.run(["taskkill", "/IM", proc_name, "/F"],
                   capture_output=True, text=True, timeout=20)
    time.sleep(2)

    # 2. 导出 Cookie
    out = os.path.join(BASE_DIR, f"cookies_{browser}.txt")
    try:
        if FROZEN:
            cmd = [sys.executable, "--export-cookies", browser]
        else:
            cmd = [sys.executable, os.path.join(BASE_DIR, "export_cookies.py"), browser]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90,
                           encoding="utf-8", errors="replace")
        success = os.path.exists(out)
    except Exception as e:
        success = False
        r = None

    # 3. 重启浏览器并恢复标签页
    exe = _find_browser_exe(browser)
    if exe:
        try:
            subprocess.Popen([exe, "--restore-last-session"],
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except Exception:
            pass

    if success:
        return jsonify({"ok": True})
    msg = (r.stdout + r.stderr) if r else ""
    low = msg.lower()
    if "dpapi" in low or "app-bound" in low or "app_bound" in low:
        return jsonify({"ok": False, "code": "COOKIE_ENCRYPT",
                        "error": ZH_ERRORS["COOKIE_ENCRYPT"]})
    if "Could not copy" in msg or "cookie database" in low:
        return jsonify({"ok": False, "error": "浏览器未能完全关闭，请稍后重试一次"})
    return jsonify({"ok": False, "error": msg.strip()[-200:] or "未知错误"})


@app.route("/api/import_cookies", methods=["POST"])
def api_import_cookies():
    """导入用户用浏览器扩展导出的 Netscape cookies.txt，保存为 cookies_manual.txt"""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "未选择文件"})
    content = f.stream.read().decode("utf-8", errors="replace")
    lines = [l for l in content.splitlines() if l.strip() and not l.startswith("#")]
    # Netscape 格式：每行 7 个制表符分隔字段，且包含域名与 cookie 名
    valid = [l for l in lines if "\t" in l and l.split("\t")[0].lstrip(".").count(".") >= 1]
    if not valid:
        return jsonify({"ok": False, "error": "文件格式不对：应为浏览器扩展导出的 Netscape cookies.txt 格式"})
    out = os.path.join(BASE_DIR, "cookies_manual.txt")
    with open(out, "w", encoding="utf-8") as fp:
        fp.write("# Netscape HTTP Cookie File\n" + content)
    return jsonify({"ok": True, "cookies": len(valid)})


@app.route("/api/paste_cookies", methods=["POST"])
def api_paste_cookies():
    """直接粘贴 Cookie，免文件导入。支持两种格式：
    ① F12 复制的 Cookie 请求头（name=value; name2=value2）
    ② 浏览器扩展导出的 Netscape cookies.txt 原文
    Cookie 头本身不带域名，用当前解析链接的域名补全归属网站。"""
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    url = (data.get("url") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "没有粘贴任何内容"})

    # ① 看起来是 Netscape cookies.txt（与导入通道同样的校验）
    lines = [l for l in text.splitlines() if l.strip() and not l.startswith("#")]
    netscape = [l for l in lines if "\t" in l and
                l.split("\t")[0].lstrip(".").count(".") >= 1]
    if netscape and len(netscape) >= max(1, len(lines) // 2):
        out = os.path.join(BASE_DIR, "cookies_manual.txt")
        with open(out, "w", encoding="utf-8") as fp:
            fp.write("# Netscape HTTP Cookie File\n" + text)
        return jsonify({"ok": True, "cookies": len(netscape)})

    # ② Cookie 请求头：按分号拆键值对
    pairs = []
    for seg in text.replace("\n", ";").split(";"):
        seg = seg.strip()
        if not seg or "=" not in seg:
            continue
        name, _, value = seg.partition("=")
        name = name.strip()
        value = value.strip()
        if name and " " not in name and value:
            pairs.append((name, value))
    if not pairs:
        return jsonify({"ok": False,
                        "error": "没认出 Cookie：请粘贴 F12 里 Cookie 冒号后面的整串（形如 a=1; b=2），或扩展导出的 cookies.txt 内容"})

    # 用解析链接的域名确定 Cookie 归属网站
    host = ""
    if url:
        try:
            host = urllib.parse.urlsplit(
                url if "://" in url else "https://" + url).hostname or ""
        except ValueError:
            host = ""
    if not host:
        return jsonify({"ok": False,
                        "error": "请先在上方输入框粘贴要解析的链接（需要用它确定 Cookie 属于哪个网站）"})
    core = host[4:] if host.startswith("www.") else host
    domain = "." + core
    secure = "TRUE" if url.startswith("https") else "FALSE"
    rows = [f"{domain}\tTRUE\t/\t{secure}\t2147483644\t{n}\t{v}"
            for n, v in pairs]
    out = os.path.join(BASE_DIR, "cookies_manual.txt")
    with open(out, "w", encoding="utf-8") as fp:
        fp.write("# Netscape HTTP Cookie File\n" + "\n".join(rows) + "\n")
    return jsonify({"ok": True, "cookies": len(pairs), "domain": core})


@app.route("/api/get_download_dir")
def api_get_download_dir():
    return jsonify({"ok": True, "dir": DOWNLOAD_DIR})


@app.route("/api/set_download_dir", methods=["POST"])
def api_set_download_dir():
    data = request.get_json(silent=True) or {}
    ok, err = _apply_download_dir(data.get("dir") or "")
    if not ok:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "dir": DOWNLOAD_DIR})


def _apply_download_dir(d: str):
    """校验并切换下载目录（持久化），返回 (ok, 错误信息)"""
    global DOWNLOAD_DIR
    d = os.path.abspath(os.path.expanduser((d or "").strip().strip('"')))
    if not d:
        return False, "路径为空"
    try:
        os.makedirs(d, exist_ok=True)
        test = os.path.join(d, ".vyt_write_test")
        with open(test, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test)
    except Exception as e:
        return False, f"该目录不可用：{e}"
    DOWNLOAD_DIR = d
    try:
        with open(_DLDIR_CFG, "w", encoding="utf-8") as f:
            f.write(d)
    except Exception:
        pass
    return True, ""


PICK_HELPER = os.path.join(BASE_DIR, "pick_dir_helper.py")


def _win_pick_folder(title: str, initial: str):
    """
    Windows: delegate the native folder picker to pick_dir_helper.py, which
    shows IFileOpenDialog (the Explorer-style dialog with Desktop / This PC /
    drive sidebar that installers use).

    Why a separate process:
      1. The Flask worker thread may already be MTA, where IFileDialog fails;
      2. a modal dialog would block the request thread for minutes;
      3. the helper can use the foreground window as owner, so the dialog
         always appears in front of the browser.
    Returns (status, path): ("ok", path) / ("cancel", "") / ("error", reason)
    """
    if FROZEN:
        # A packaged build has no loose .py files, so re-launch the same exe
        # with a marker argument; __main__ below hands it to pick_dir_helper.
        cmd = [sys.executable, "--pick-dir", title or "", initial or ""]
    elif os.path.exists(PICK_HELPER):
        cmd = [sys.executable, PICK_HELPER, title or "", initial or ""]
    else:
        return "error", "pick_dir_helper.py is missing"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600, creationflags=flags)
    except subprocess.TimeoutExpired:
        return "error", "folder picker timed out"
    except Exception as e:
        return "error", str(e)

    if r.returncode == 0:
        path = (r.stdout or b"").decode("utf-8", "replace").strip()
        if path and os.path.isdir(path):
            return "ok", path
        return "error", "no valid folder path returned"
    if r.returncode == 2:
        return "cancel", ""
    return "error", ((r.stderr or b"").decode("utf-8", "replace").strip()
                     or "system dialog failed")


def _tk_pick_dir(title: str = "Select Folder", initial: str = ""):
    """Tk fallback, only used when every native option is unavailable."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        p = filedialog.askdirectory(title=title, initialdir=initial or None)
        root.destroy()
        return ("ok", p) if p else ("cancel", "")
    except Exception as e:
        return "error", f"fallback picker unavailable: {e}"


def _native_pick_dir(title: str = "Select Folder", initial: str = ""):
    """
    Show the OS-native folder picker. Returns (status, path).
    Windows = IFileOpenDialog (Explorer style)
    macOS   = osascript choose folder (native sidebar panel)
    Linux   = zenity / kdialog (desktop native)
    """
    title = title or "Select Folder"
    initial = initial or ""
    try:
        if os.name == "nt":
            status, path = _win_pick_folder(title, initial)
            if status != "error":
                return status, path
            hint = path
            st2, p2 = _tk_pick_dir(title, initial)
            return (st2, p2) if st2 != "error" else ("error", hint)

        if sys.platform == "darwin":
            mac_title = title.replace("\\", "\\\\").replace('"', '\\"')
            script = f'set theFolder to choose folder with prompt "{mac_title}"'
            if initial and os.path.isdir(initial):
                mac_ini = os.path.expanduser(initial).replace("\\", "\\\\").replace('"', '\\"')
                script += f' default location (POSIX file "{mac_ini}")'
            script += "\nPOSIX path of theFolder"
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=600)
            p = (r.stdout or "").strip().rstrip("/")
            if p and os.path.isdir(p):
                return "ok", p
            err = (r.stderr or "").strip()
            # -128 / "User canceled" means the user cancelled, not a failure
            if r.returncode != 0 and "-128" not in err and "canceled" not in err.lower():
                return "error", (err[-160:] or "osascript failed")
            return "cancel", ""

        try:
            r = subprocess.run(
                ["zenity", "--file-selection", "--directory",
                 f"--title={title}",
                 f"--filename={initial or os.path.expanduser('~')}/"],
                capture_output=True, text=True, timeout=600)
            p = (r.stdout or "").strip()
            if p and os.path.isdir(p):
                return "ok", p
            if r.returncode == 0:
                return "cancel", ""
        except FileNotFoundError:
            pass

        try:
            r = subprocess.run(
                ["kdialog", "--getexistingdirectory", initial or os.path.expanduser("~"),
                 "--title", title],
                capture_output=True, text=True, timeout=600)
            p = (r.stdout or "").strip()
            if p and os.path.isdir(p):
                return "ok", p
            if r.returncode == 0:
                return "cancel", ""
        except FileNotFoundError:
            pass
    except Exception as e:
        return "error", str(e)
    return _tk_pick_dir(title, initial)


@app.route("/api/pick_download_dir", methods=["POST"])
def api_pick_download_dir():
    """Show the native folder picker; the chosen folder becomes the download dir."""
    data = request.get_json(silent=True) or {}
    title = data.get("title") or "Select Folder"
    status, picked = _native_pick_dir(title, DOWNLOAD_DIR)
    if status == "cancel":
        return jsonify({"ok": False, "cancelled": True, "dir": DOWNLOAD_DIR})
    if status == "error":
        print(f"[pick_dir] failed: {picked}", flush=True)
        return jsonify({"ok": False, "error": picked, "dir": DOWNLOAD_DIR}), 500
    print(f"[pick_dir] user picked: {picked}", flush=True)
    ok, err = _apply_download_dir(picked)
    if not ok:
        return jsonify({"ok": False, "error": err, "dir": DOWNLOAD_DIR}), 400
    return jsonify({"ok": True, "dir": DOWNLOAD_DIR})


@app.route("/api/open_download_dir", methods=["POST"])
def api_open_download_dir():
    try:
        if os.name == "nt":
            os.startfile(DOWNLOAD_DIR)  # noqa 打开资源管理器
        elif sys.platform == "darwin":
            subprocess.Popen(["open", DOWNLOAD_DIR])
        else:
            subprocess.Popen(["xdg-open", DOWNLOAD_DIR])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------- yt-dlp core update ----------------
# Platform extractors break whenever a site changes; a frozen build cannot
# `pip install -U yt-dlp`. So we download the newest wheel from PyPI into
# CORE_DIR (next to the app) and the next start loads it before the bundled one.
CORE_SOURCES = [
    "https://pypi.org/pypi/yt-dlp/json",
    "https://pypi.tuna.tsinghua.edu.cn/pypi/yt-dlp/json",
]


def _ver_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:4]) or (0,)


def _active_core():
    """Return (version, is_user_core)."""
    try:
        ver = yt_dlp.version.__version__
    except Exception:
        ver = "unknown"
    try:
        path = os.path.abspath(yt_dlp.__file__)
        return ver, path.startswith(os.path.abspath(CORE_DIR) + os.sep)
    except Exception:
        return ver, False


def _http_get(url, proxy=None, timeout=25):
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(url, headers={"User-Agent": "universal-media-extractor"})
    with opener.open(req, timeout=timeout) as r:
        return r.read()


def _latest_core(proxy=None):
    last = None
    for src in CORE_SOURCES:
        try:
            data = json.loads(_http_get(src, proxy).decode("utf-8"))
            wheel = None
            for f in data.get("urls", []):
                if f.get("packagetype") == "bdist_wheel" and f["filename"].endswith(".whl"):
                    wheel = f["url"]
                    break
            if wheel:
                return data["info"]["version"], wheel
        except Exception as e:
            last = e
    raise RuntimeError(str(last or "PyPI unreachable"))


def _install_core(wheel_url, proxy=None):
    """Download the wheel and atomically replace CORE_DIR/yt_dlp."""
    tmpdir = tempfile.mkdtemp(prefix="ytdlp_core_")
    try:
        whl = os.path.join(tmpdir, "core.whl")
        with open(whl, "wb") as f:
            f.write(_http_get(wheel_url, proxy, timeout=180))
        stage = os.path.join(tmpdir, "stage")
        with zipfile.ZipFile(whl) as z:
            for name in z.namelist():
                if not name.startswith("yt_dlp/"):
                    continue
                target = os.path.abspath(os.path.join(stage, name))
                if not target.startswith(os.path.abspath(stage) + os.sep):
                    raise RuntimeError("unsafe path inside wheel")
                z.extract(name, stage)
        staged_pkg = os.path.join(stage, "yt_dlp")
        if not os.path.isdir(staged_pkg):
            raise RuntimeError("wheel does not contain the yt_dlp package")
        ver_file = os.path.join(staged_pkg, "version.py")
        with open(ver_file, encoding="utf-8") as f:
            m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", f.read())
        if not m:
            raise RuntimeError("cannot read version from wheel")

        new_core, old_core = CORE_DIR + ".new", CORE_DIR + ".old"
        shutil.rmtree(new_core, ignore_errors=True)
        shutil.rmtree(old_core, ignore_errors=True)
        os.makedirs(new_core)
        shutil.copytree(staged_pkg, os.path.join(new_core, "yt_dlp"))
        if os.path.isdir(CORE_DIR):
            try:
                os.rename(CORE_DIR, old_core)
            except OSError:
                # files may be in use on Windows: fall back to a plain remove
                shutil.rmtree(CORE_DIR, ignore_errors=True)
        os.rename(new_core, CORE_DIR)
        shutil.rmtree(old_core, ignore_errors=True)
        return m.group(1)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.route("/api/core_status")
def api_core_status():
    ver, user_core = _active_core()
    return jsonify({"ok": True, "version": ver, "user_core": user_core,
                    "frozen": FROZEN})


@app.route("/api/update_core", methods=["POST"])
def api_update_core():
    body = request.get_json(silent=True) or {}
    proxy = (body.get("proxy") or "").strip() or None
    cur, _ = _active_core()
    try:
        latest, wheel = _latest_core(proxy)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "version": cur}), 502
    if _ver_tuple(latest) <= _ver_tuple(cur):
        return jsonify({"ok": True, "up_to_date": True, "version": cur, "latest": latest})
    try:
        _install_core(wheel, proxy)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "version": cur}), 500
    print(f"[core] yt-dlp updated {cur} -> {latest} (restart to apply)", flush=True)
    return jsonify({"ok": True, "up_to_date": False, "version": latest,
                    "previous": cur, "restart": True})


@app.route("/api/save_cover", methods=["POST"])
def api_save_cover():
    """
    保存封面到「下载地址」里以标题命名的文件夹（与该链接的视频/图片/音乐同处）。
    优先接收浏览器上传的 blob（前端已实测像素挑出的最大图，字节最可靠）；
    也兼容传 url 由服务端代下。
    """
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    jdata = request.get_json(silent=True) or {}
    title = (request.form.get("title") or jdata.get("title") or "cover").strip() or "cover"
    ext = (request.form.get("ext") or jdata.get("ext") or "jpg").strip().lstrip(".").lower()
    if ext not in ("jpg", "jpeg", "png", "webp", "gif", "avif", "bmp"):
        ext = "jpg"
    base = sanitize(title)[:80] or "cover"
    dest_dir = title_out_dir(base, "cover")
    path = os.path.join(dest_dir, f"{base}_封面.{ext}")
    stem, suf = os.path.splitext(path)
    i = 1
    while os.path.exists(path):          # 同名不覆盖
        path = f"{stem}({i}){suf}"
        i += 1

    try:
        f = request.files.get("file")
        if f is not None:
            f.save(path)
        else:
            url = (jdata.get("url") or "").strip()
            if not url:
                return jsonify({"ok": False, "error": "empty"}), 400
            webpage = (jdata.get("webpage") or "").strip()
            ref = webpage or url
            if "doubanio.com" in url:
                ref = "https://movie.douban.com/"   # 豆瓣图床只认豆瓣 Referer
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Referer": ref})
            with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as fp:
                shutil.copyfileobj(r, fp)
    except Exception as e:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        return jsonify({"ok": False, "error": str(e)[:200]}), 500

    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return jsonify({"ok": False, "error": "empty file"}), 500
    _upscale_small_file(path)     # 「图片放大」：封面太小则原格式放大重写
    return jsonify({"ok": True, "path": path, "dir": dest_dir,
                    "name": os.path.basename(path)})


@app.route("/api/save_lyrics", methods=["POST"])
def api_save_lyrics():
    """导出歌词为 .lrc 文件（带翻译时输出双语逐行对照），存到以标题命名的文件夹"""
    data = request.get_json(silent=True) or {}
    lrc = (data.get("lyrics") or "").strip()
    if not lrc:
        return jsonify({"ok": False, "error": "no_lyrics"}), 400
    trans = (data.get("lyrics_trans") or "").strip()
    base = sanitize((data.get("title") or "lyrics").strip())[:80] or "lyrics"
    dest_dir = title_out_dir(base, "lyrics")
    path = os.path.join(dest_dir, f"{base}.lrc")
    try:
        with open(path, "w", encoding="utf-8-sig") as fp:   # BOM 便于 Windows 记事本正确显示
            fp.write(_lrc_merge(lrc, trans))
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    return jsonify({"ok": True, "path": path, "dir": dest_dir,
                    "name": os.path.basename(path)})


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(silent=True) or {}
    url = extract_url(data.get("url") or "")
    kind = data.get("kind", "video")          # video / audio
    height = int(data.get("height") or 0)     # 分辨率高度上限，0 = 最佳
    proxy = data.get("proxy") or ""
    cookie_browser = data.get("cookie_browser") or ""
    # 影视站选集：用户选定的某集播放页链接（仅影视站用，其他平台忽略）
    play_url = (data.get("play_url") or "").strip()
    # 批量下载：漫画全部话 / 影视全部集（前端「下载全话 / 下载全集」按钮）
    batch = bool(data.get("all"))
    if not url:
        return jsonify({"error": "请粘贴链接", "code": "EMPTY_URL"}), 400
    if not re.match(r"^https?://", url):
        url = "https://" + url

    task_id = uuid.uuid4().hex[:12]
    with TASKS_LOCK:
        TASKS[task_id] = {
            "status": "starting", "progress": 0, "url": url,
            "kind": kind, "height": height, "proxy": proxy,
            "created": time.time(),
        }
    if SPH_RE.match(url):
        t = threading.Thread(target=run_sph, args=(task_id, url, kind, proxy),
                             daemon=True)
    elif _is_xhs(url):
        t = threading.Thread(target=run_xhs,
                             args=(task_id, url, kind, height, proxy, cookie_browser),
                             daemon=True)
    elif _is_douyin(url):
        t = threading.Thread(target=run_douyin,
                             args=(task_id, url, kind, height, proxy, cookie_browser),
                             daemon=True)
    elif _is_manga(url):
        # 单话下载要带上前端选中的话（play_url），否则永远只下 chapters[0]
        target = run_manga_batch if batch else run_manga
        args = (task_id, url) if batch else (task_id, url, play_url)
        t = threading.Thread(target=target, args=args, daemon=True)
    elif batch and (_film_site(url) or _film_looks_like(url)):
        t = threading.Thread(target=_film_download_batch, args=(task_id, url),
                             daemon=True)
    else:
        t = threading.Thread(target=run_download,
                             args=(task_id, url, kind, height, proxy,
                                   cookie_browser, play_url),
                             daemon=True)
    t.start()
    return jsonify({"task_id": task_id})


@app.route("/api/progress/<task_id>")
def api_progress(task_id):
    task = TASKS.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify({k: task.get(k) for k in
                    ("status", "progress", "speed", "eta", "error",
                     "error_code", "filename", "count", "episode",
                     "ok_count", "skip_count", "fail_count", "total_count",
                     "failed")})


@app.route("/api/film_preview")
def api_film_preview():
    """影视站选集预览：按播放页链接解析该集的视频源（前端 hls.js 切集播放用）"""
    url = request.args.get("url", "")
    if not re.match(r"^https?://", url) or \
            not re.search(r"/[a-z0-9_-]*play[a-z0-9_-]*/[^?\s]+\.html", url, re.I):
        return jsonify({"error": "invalid url"}), 400
    try:
        m3u8 = _film_m3u8(url)
    except Exception:
        m3u8 = ""
    if not m3u8:
        return jsonify({"error": "该集没有可用的视频源（线路可能已失效）"}), 404
    return jsonify({"preview_url": m3u8})


@app.route("/api/film_cover")
def api_film_cover():
    """封面快照：只抓详情页/播放页 HTML 取片名+封面（不碰视频源和元数据），
    让结果卡先显示出来；完整解析随后由 /api/analyze 补齐。非影视站返回 film=False"""
    url = extract_url(request.args.get("url", ""))
    if url and not re.match(r"^https?://", url):
        url = "https://" + url
    if not re.match(r"^https?://", url):
        return jsonify({"film": False})
    site = _film_site(url)
    if site:
        origin, name = site
    elif _film_looks_like(url):
        mh = re.match(r"https?://([^/?#]+)", url, re.I)
        if not mh:
            return jsonify({"film": False})
        origin = "https://" + mh.group(1).lower()
        name = re.sub(r"^www\.", "", origin.split("//", 1)[1])
    else:
        return jsonify({"film": False})
    try:
        if re.search(r"/[a-z0-9_-]*play[a-z0-9_-]*/[^?\s]+\.html", url, re.I):
            ptitle, pcover = _film_title_cover(_film_html(url))
            title, cover = ptitle, pcover
            # 播放页反查详情页拿更准的封面（与 _film_resolve 同一套规则）
            mid = re.search(r"/(\d+)(?:-\d+)*\.html", url)
            cands = re.findall(
                r'href="([^"]*?/[a-z0-9_-]*(?:detail|video|vod)[a-z0-9_-]*/\d+\.html)"',
                _film_html(url), re.I)
            durl = None
            if mid:
                durl = next((c for c in cands
                             if re.search(r"/" + mid.group(1) + r"\.html$", c)), None)
            if durl is None and cands:
                durl = cands[0]
            if durl:
                if not durl.startswith("http"):
                    durl = origin + durl
                dtitle, dcover = _film_title_cover(_film_html(durl))
                title, cover = dtitle or title, dcover or cover
        else:
            title, cover = _film_title_cover(_film_html(url))
        if not title:
            return jsonify({"film": False})
        cover = _douban_poster(title) or cover
        return jsonify({"film": True, "title": title, "cover": cover,
                        "platform": name})
    except Exception:
        return jsonify({"film": False})


@app.route("/api/manga_preview")
def api_manga_preview():
    """漫画选话预览：返回所选话的全部页图（前端刷新图集网格，同影视选集逻辑）"""
    url = request.args.get("url", "")
    if not _is_manga_chapter(url):
        return jsonify({"error": "invalid url"}), 400
    try:
        name, chapter, images = _manga_parse_chapter(url)
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 400
    return jsonify({"title": _manga_file_title(name, chapter),
                    "image_count": len(images), "images": images[:24]})


@app.route("/api/image_proxy")
def api_image_proxy():
    """图集缩略图代理：带上当前代理与 Cookie 取图，避免平台防盗链导致预览空白"""
    url = request.args.get("url", "")
    if not re.match(r"^https?://", url):
        return Response("", status=400)
    opts = base_opts(request.args.get("proxy", ""), request.args.get("cookie", ""))
    ref = request.args.get("ref", "") or url
    if "doubanio.com" in url:
        # 豆瓣图床只认豆瓣自己的 Referer，其它一律 403/418
        ref = "https://movie.douban.com/"
    if _manga_site(url) or _manga_site(ref):
        # 漫画站图床国内直连，走代理反而连不上
        opts = base_opts("", request.args.get("cookie", ""))
    try:
        ydl = yt_dlp.YoutubeDL(opts)
        req = sanitized_Request(url, headers={
            "User-Agent": UA,
            "Referer": ref,
        })
        resp = ydl.urlopen(req)
        ctype = resp.headers.get("Content-Type") or "image/jpeg"
        return Response(iter(lambda: resp.read(1 << 16), b""),
                        content_type=ctype,
                        headers={"Cache-Control": "private, max-age=600"})
    except Exception:
        return Response("", status=404)


@app.route("/api/video_preview")
def api_video_preview():
    """
    视频在线预览代理：浏览器 <video> 无法直接播平台流（防盗链/Referer/分片），
    由后端带上 UA/Referer/Cookie/代理转发，并透传 Range 请求支持拖动进度条。
    """
    url = request.args.get("url", "")
    if not re.match(r"^https?://", url):
        return Response("", status=400)
    ref = request.args.get("ref", "")
    # 影视站（详情/播放页 ref）：均为国内直连站点，走代理反而连不上 CDN，强制直连
    direct = bool(ref) and bool(_film_site(ref) or _film_looks_like(ref))
    opts = base_opts("" if direct else request.args.get("proxy", ""),
                     request.args.get("cookie", ""))
    headers = {"User-Agent": UA}
    if ref:
        headers["Referer"] = ref
    rng = request.headers.get("Range")
    if rng:
        headers["Range"] = rng
    try:
        ydl = yt_dlp.YoutubeDL(opts)
        resp = ydl.urlopen(sanitized_Request(url, headers=headers))
    except Exception:
        return Response("", status=404)
    status = getattr(resp, "status", 200) or 200

    # HLS 播放列表（m3u8）：把列表里的分片/子列表/密钥地址全部改写成本代理的
    # 地址，让前端 hls.js 全程经本地代理取流（绕过防盗链 Referer 与 CORS）
    if "mpegurl" in (resp.headers.get("Content-Type") or "").lower() \
            or url.split("?")[0].lower().endswith(".m3u8"):
        from urllib.parse import urljoin, urlencode
        try:
            text = resp.read().decode("utf-8", "ignore")
        except Exception:
            return Response("", status=404)
        args = request.args.to_dict()

        def _px(u2: str) -> str:
            return "/api/video_preview?" + urlencode({**args, "url": u2})

        lines = []
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("#"):
                if "URI=" in s:      # AES-128 密钥等内嵌 URI 也要走代理
                    line = re.sub(r'URI="([^"]+)"',
                                  lambda m2: 'URI="' + _px(urljoin(url, m2.group(1))) + '"',
                                  line)
                lines.append(line)
            else:                    # 分片或子播放列表（可能是相对地址）
                lines.append(_px(urljoin(url, s)))
        return Response("\n".join(lines) + "\n",
                        content_type="application/vnd.apple.mpegurl")

    def gen():
        try:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                yield chunk
        except Exception:
            pass

    out_headers = {}
    for k in ("Content-Range", "Content-Length", "Accept-Ranges"):
        v = resp.headers.get(k)
        if v:
            out_headers[k] = v
    ctype = resp.headers.get("Content-Type") or "video/mp4"
    if "octet-stream" in ctype:
        # m4s/m4a 等平台自定义类型：本质都是 mp4 容器，<video>/<audio> 可直接播
        ctype = "video/mp4"
    return Response(gen(), status=status, content_type=ctype, headers=out_headers)



@app.route("/api/file/<task_id>")
def api_file(task_id):
    task = TASKS.get(task_id)
    if not task or task.get("status") != "done":
        return jsonify({"error": "文件尚未就绪"}), 404
    return send_file(task["filepath"], as_attachment=True,
                     download_name=task["filename"])


@app.route("/api/reveal_file", methods=["POST"])
def api_reveal_file():
    """
    Open the system file manager with the finished file selected.

    The file is already saved in DOWNLOAD_DIR, so re-downloading it through
    the browser would only create a duplicate in the browser's own Downloads
    folder — revealing it in place is the correct UX.
    """
    task = TASKS.get((request.get_json(silent=True) or {}).get("task_id", ""))
    if not task or task.get("status") != "done":
        return jsonify({"ok": False, "error": "文件尚未就绪"}), 404
    path = task.get("filepath") or ""
    # 图集/漫画散图：filepath 指向存放文件夹，直接打开文件夹
    if path and os.path.isdir(path):
        path = ""
        try:
            if os.name == "nt":
                os.startfile(task.get("filepath"))  # noqa
                return jsonify({"ok": True})
            opener_cmd = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener_cmd, task.get("filepath")])
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    try:
        if os.name == "nt":
            if path and os.path.exists(path):
                # explorer always returns a non-zero exit code, don't check it
                subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"',
                                 shell=True)
            else:
                os.startfile(DOWNLOAD_DIR)  # noqa file already cleaned up
        elif sys.platform == "darwin":
            if path and os.path.exists(path):
                subprocess.Popen(["osascript", "-e",
                    f'tell application "Finder" to reveal '
                    f'(POSIX file "{path}" as alias)'])
                subprocess.Popen(["osascript", "-e",
                    'tell application "Finder" to activate'])
            else:
                subprocess.Popen(["open", DOWNLOAD_DIR])
        else:
            subprocess.Popen(["xdg-open",
                              os.path.dirname(path) if path else DOWNLOAD_DIR])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# 定期清理 2 小时前的旧任务文件
def cleaner():
    while True:
        time.sleep(1800)
        now = time.time()
        with TASKS_LOCK:
            expired = [tid for tid, t in TASKS.items() if now - t["created"] > 7200]
            for tid in expired:
                shutil.rmtree(os.path.join(DOWNLOAD_DIR, tid), ignore_errors=True)
                TASKS.pop(tid, None)


threading.Thread(target=cleaner, daemon=True).start()

if __name__ == "__main__":
    # Packaged build: the exe doubles as the folder-picker helper process.
    if len(sys.argv) > 2 and sys.argv[1] == "--pick-dir":
        import pick_dir_helper
        sys.argv = [sys.argv[0], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ""]
        sys.exit(pick_dir_helper.main())

    if len(sys.argv) > 2 and sys.argv[1] == "--export-cookies":
        import export_cookies
        sys.argv = [sys.argv[0], sys.argv[2]]
        sys.exit(export_cookies.main())

    port = int(os.environ.get("PORT", "8765"))
    url = f"http://127.0.0.1:{port}"

    # --- 输出管道保险（重要，勿删）---
    # 当 stdout/stderr 是管道（被其他进程拉起、日志无人读取，如 nohup/任务计划/
    # 某些 IDE）时，一旦下游停止读取，64KB 缓冲写满后，任何往日志写一行字
    # 的线程都会在内核里永久阻塞（EventPairLow）——下载/转换线程直接冻死，
    # 且毫无报错。因此：非 TTY 场景把输出重定向到日志文件，绝不依赖外部读者。
    for _idx, _h in ((1, sys.stdout), (2, sys.stderr)):
        try:
            _tty = _h and _h.isatty()
        except Exception:
            _tty = False
        if not _tty:
            _log = open(os.path.join(BASE_DIR, "server.log"), "a", buffering=1,
                        encoding="utf-8", errors="replace")
            os.dup2(_log.fileno(), _idx)
    print("=" * 56)
    print(f" 万能素材提取工具 / Universal Media Extractor")
    print(f" 本地地址 Local: {url}")
    print(" 关闭此窗口即停止服务 · Close this window to stop")
    print("=" * 56)
    # Dev mode: watch source files and restart automatically, so editing
    # app.py / templates does not require closing the window every time.
    dev = "--dev" in sys.argv or os.environ.get("DEV") == "1"
    if dev:
        print(" 开发模式 Dev mode: 改动代码后自动重启 · auto-reload on save")
        print("=" * 56)
    if os.environ.get("NO_BROWSER") != "1":
        # In dev mode the reloader child re-runs this block on every code
        # change; only the parent process (WERKZEUG_RUN_MAIN unset) opens
        # the browser, so it happens exactly once.
        if not dev or os.environ.get("WERKZEUG_RUN_MAIN") != "true":
            threading.Timer(1.2, lambda: __import__("webbrowser").open(url)).start()
    # In dev mode also watch the template, so editing the HTML restarts too.
    extra = [os.path.join(RES_DIR, "templates", "index.html")] if dev else None
    app.run(host="127.0.0.1", port=port, threaded=True,
            use_reloader=dev, extra_files=extra)
