"""漫画解析离线自检：不联网，纯逻辑回归。

每个用例都对应一个真实踩过的坑，改漫画相关代码后先跑这个：

    python tests/test_manga_parse.py

覆盖：
1. imgBaseUrl 拼接表达式求值（|| 真值语义、括号、多段拼接）
   -> 坑：('' || '1.示例目录') 被求值成空，整话图片 URL 变 ...//1.webp 全 404
2. 页图 URL 结构校验（空段/空格/无扩展名）
3. 标题切成 (漫画名, 话名)
   -> 坑：h1 是站点 logo 时漫画名被取成站名；名字与话名相同被清空
4. 章节链接提取
   -> 坑：详情页自链（标题含「第N節」）被当成章节页，解析必失败
5. 通用页图提取（目录分组 + 数字排序）
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 允许在无 GUI/无网络环境导入 app（模块级只依赖第三方库，不启动服务）
import app  # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        print("       got :", repr(got))
        print("       want:", repr(want))
        FAILED.append(name)


def check_true(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  ({detail})"))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------- 1. JS 求值
js_cases = [
    # 真实案例（某站空默认段故障）：空默认段必须被 || 兜住
    ("'https://x/mh/h/' + ('' || '1.示例目录') + '/'", "https://x/mh/h/1.示例目录/"),
    # 真实案例（呪術廻戦）：uuid 存在时取 uuid
    ("'https://x/mh/h/' + ('uuid-123' || '第271話') + '/'", "https://x/mh/h/uuid-123/"),
    # 纯拼接
    ("'a/' + 'b' + '/c'", "a/b/c"),
    # 两段都为空的极端情况：保持原样（校验层会拦住）
    ("'base/' + ('' || '') + '/'", "base//"),
    # 多个括号组
    ("'x' + ('a' || 'b') + ('c' || 'd')", "xac"),
    # 带换行/空格的表达式（换行在字符串外，与真实页面一致）
    ("'https://y/p/' +\n  ('z' || 'q') + '/'", "https://y/p/z/"),
]
for expr, want in js_cases:
    check("js_concat " + expr[:46], app._js_concat_strings(expr), want)

# 关键回归：绝不能出现空目录段（这就是某站的线上故障）
check_true("js_concat 不产生空目录段",
           "//1.webp" not in app._js_concat_strings(
               "'https://x/mh/h/' + ('' || '1.示例目录') + '/'") + "1.webp")

# ---------------------------------------------------------- 2. URL 结构校验
url_cases = [
    ("https://image1.manga.example.com/mh/h/1.示例目录/1.webp", True),
    ("https://image1.manga.example.com/mh/h//1.webp", False),      # 空段故障形态
    ("https://a.com/x/ 1.jpg", False),                            # 空格
    ("https://a.com/x/1", False),                                 # 无扩展名
    ("/relative/1.jpg", False),                                   # 相对路径
    ("", False),
]
for u, want in url_cases:
    check("url_ok " + (u or "<empty>")[:52], app._manga_url_ok(u), want)

# ------------------------------------------------------------ 3. 标题切分
title_cases = [
    # 详情页：标题就是作品名，站点名在最后一段
    ("<title>第七節課是忍者修行！ - 漫画库</title>"
     "<h1>第七節課是忍者修行！</h1>",
     ("第七節課是忍者修行！", "第七節課是忍者修行！")),
    # 普通详情页
    ("<title>凌晨兩點的蠢女人 - 漫画库</title><h1>凌晨兩點的蠢女人</h1>",
     ("凌晨兩點的蠢女人", "")),
    # 章节页：作品名 + 话名 + 站名
    ("<title>呪術廻戦 第271話 - 漫画库</title>", ("呪術廻戦", "第271話")),
    # 章节页：话名在前
    ("<title>第12話 - 妖神記 - 包子漫畫</title>", ("妖神記", "第12話")),
]
for html, want in title_cases:
    check("title " + html[:40], app._manga_title_parts(html), want)

# -------------------------------------------------------- 4. 章节链接提取
_detail_html = """
<a href="/comic/2499.html">第七節課是忍者修行！</a>
<a href="/comic/2499.html">收藏</a>
<a href="/chapter/2499-1.html">第01話</a>
<a href="/chapter/2499-2.html">第02話</a>
<a href="/chapter/2499-3.html">第03話</a>
<a href="/chapter/2499-2.html">第02話</a>
<a href="/chapter/2499-1.html">上一话</a>
<a href="https://other.com/comic/1.html">推荐</a>
"""
links = app._manga_chapter_links(_detail_html, "https://www.manga.example.com/comic/2499.html")
labels = [c["label"] for c in links]
check("chapter_links 标签", labels, ["第01話", "第02話", "第03話"])
check_true("chapter_links 无自链",
           all(c["url"].endswith(("-1.html", "-2.html", "-3.html")) for c in links),
           str([c["url"] for c in links]))

# ------------------------------------------------- 5. 通用页图提取（含分组）
_gen_html = """
<img src="/static/logo.png">
<img data-src="https://cdn.x/mh/abc/1.jpg"><img data-src="https://cdn.x/mh/abc/2.jpg">
<img data-src="https://cdn.x/mh/abc/10.jpg">
<img src="https://cdn.x/banner/ad.jpg">
"""
imgs = app._manga_images_from_html(_gen_html)
check("generic images 取最大目录组", len(imgs), 3)
check("generic images 按页码排序",
      [i["url"].rsplit("/", 1)[-1] for i in imgs], ["1.jpg", "2.jpg", "10.jpg"])

print()
if FAILED:
    print(f"{len(FAILED)} FAILED:", ", ".join(FAILED))
    sys.exit(1)
print("ALL PASS")
