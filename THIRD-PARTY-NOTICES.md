# Third-Party Notices / 第三方组件声明

本文件列出 Universal Media Extractor 用到或分发的所有第三方组件、其版权归属、许可证及法定信息。
本项目自身代码以 [MIT](LICENSE) 许可证发布，与下列组件的许可证相互独立。

This file lists all third-party components used or distributed with Universal Media Extractor,
their copyright holders, licenses, and legal notices. This project's own code is licensed
under the [MIT License](LICENSE), which is independent of the licenses below.

---

## 1. 内嵌于仓库源码的第三方代码 / Bundled in the repository source

### hls.js v1.5.20（`assets/hls.light.min.js`）

- 用途 / Purpose: m3u8（HLS）分片流的浏览器内预览 / in-browser preview of HLS streams
- 版权 / Copyright: Copyright 2017 Dailymotion and hls.js contributors
- 许可证 / License: **Apache License 2.0**
- 源码 / Source: https://github.com/video-dev/hls.js
- 许可证全文 / Full license text: [licenses/LICENSE.apache-2.0.txt](licenses/LICENSE.apache-2.0.txt)

---

## 2. 运行时依赖（pip 安装，源码不内嵌于仓库）/ Runtime dependencies (installed via pip)

以下组件以 `requirements.txt` 声明依赖、由用户环境自行安装，本仓库不内嵌其源码。
Declared in `requirements.txt` and installed by the user's environment; this repository
does not embed their source code.

| 组件 / Component | 用途 / Purpose | 许可证 / License | 源码 / Source |
|---|---|---|---|
| Flask（含 Werkzeug、Jinja2） | 本地 Web 服务 / local web server | BSD-3-Clause | https://github.com/pallets/flask |
| yt-dlp | 视频解析核心 / extraction engine | **Unlicense（公有领域 / public domain）** | https://github.com/yt-dlp/yt-dlp |
| imageio-ffmpeg | 提供 ffmpeg 可执行文件 / ships ffmpeg binary | BSD-2-Clause（其内嵌的 ffmpeg 二进制为 GPL v3，见第 3 节） | https://github.com/imageio/imageio-ffmpeg |
| websocket-client | 抖音浏览器 CDP 通道 / Douyin CDP channel | Apache-2.0 | https://github.com/websocket-client/websocket-client |
| Pillow | 图片放大与转 PNG / image upscaling & PNG conversion | MIT-CMU (HPND) | https://github.com/python-pillow/Pillow |
| mutagen | mp3 歌词写入 / ID3 lyrics embedding | **GPL-2.0-or-later**（可按 GPLv3 条款分发，见第 3 节） | https://github.com/quodlibet/mutagen |

---

## 3. 二进制发行版（PyInstaller 打包的 exe / .app）额外包含 / Additional components inside binary releases

通过 GitHub Releases 分发的可执行文件由 PyInstaller 打包，除第 1、2 节组件外还内嵌：

### FFmpeg（来自 imageio-ffmpeg wheel 的 essentials 构建 binaries）

- 版权 / Copyright: Copyright (c) the FFmpeg developers and contributors
- 许可证 / License: **GNU GPL v3**（gyan.dev essentials 构建，启用 `--enable-gpl`，含 libx264、libx265 等 GPL 组件）
- 许可证全文 / Full license text: [licenses/LICENSE.GPL-3.0.txt](licenses/LICENSE.GPL-3.0.txt)
- 源码获取 / Corresponding source: https://www.gyan.dev/ffmpeg/builds/ 及 https://ffmpeg.org/download.html
  （ essentials 构建完整构建脚本与补丁可在上述页面获取 / full build scripts and patches are published there ）

### Python 3.13（运行时本体 / interpreter runtime）

- 版权 / Copyright: Copyright (c) 2001-2026 Python Software Foundation
- 许可证 / License: **Python Software Foundation License Version 2 (PSF-2.0)**
- 源码 / Source: https://www.python.org/

### mutagen（经 PyInstaller 打入二进制 / bundled by PyInstaller)

- 许可证 / License: **GPL-2.0-or-later**。依据其许可证第 2 条「or (at your option) any later version」，
  本项目按 **GPL v3** 条款分发该组件，许可证全文见 [licenses/LICENSE.GPL-3.0.txt](licenses/LICENSE.GPL-3.0.txt)。
  Distributed under GPL v3 terms (per its "or later version" clause).

### PyInstaller 引导程序 / bootloader

- 许可证 / License: GPL-2.0 **with a special exception** —— 该例外明确允许将 PyInstaller 打包输出
  以任意许可证再分发，故打包产物不因此变为 GPL。
  The bootloader ships with a linking exception that explicitly permits redistributing
  PyInstaller's output under any license; the packaged output is NOT subject to GPL.
- 源码 / Source: https://github.com/pyinstaller/pyinstaller

---

## 4. 合规说明 / Compliance notes

1. **仓库（GitHub 源码）**：唯一内嵌的第三方代码是 hls.js（Apache-2.0，声明见上）。pip 依赖以
   声明方式引用，源码由用户环境获取。
2. **二进制发行版**：内嵌的 GPL 组件（FFmpeg、mutagen）按 GPL 要求提供许可证全文与本声明中
   列出的源码获取途径；其余组件（BSD/MIT/Apache）按要求保留版权与许可声明（即本文件）。
3. 本项目为**聚合作品**（mere aggregation）：各第三方组件保持其原始许可证，MIT 许可证仅适用于
   本项目自身编写的代码。
4. 如有任何许可问题请提交 Issue / For any licensing questions, please open an issue.
