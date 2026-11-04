// 整页冒烟：DOM 桩 + 间接 eval 整段 <script>，再手动触发预览相关函数
const fs = require("fs");
const html = fs.readFileSync("templates/index.html", "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function mkEl(id) {
  const el = {
    id, style: {}, dataset: {}, children: [], value: "",
    classList: { add(){}, remove(){}, contains(){ return false; }, toggle(){} },
    addEventListener(){}, removeEventListener(){},
    appendChild(c){ this.children.push(c); return c; },
    prepend(c){ this.children.unshift(c); return c; },
    remove(){}, removeAttribute(){}, setAttribute(){},
    querySelector(){ return mkEl(id + "-q"); },
    querySelectorAll(){ return []; },
    closest(){ return null; },
    getContext(){ return null; },
    scrollIntoView(){},
    focus(){}, click(){},
    pause(){}, load(){}, play(){ return Promise.resolve(); },
    getBoundingClientRect(){ return { left:0, top:0, width:100, height:100 }; },
  };
  return el;
}
const els = {};
global.document = {
  getElementById(id){ return els[id] || (els[id] = mkEl(id)); },
  createElement(t){ return mkEl("created-" + t); },
  querySelectorAll(){ return []; },
  querySelector(){ return null; },
  addEventListener(){}, removeEventListener(){},
  body: mkEl("body"),
  documentElement: mkEl("html"),
};
global.window = {
  innerWidth: 1280, innerHeight: 800,
  addEventListener(){}, removeEventListener(){},
  getComputedStyle(){ return { getPropertyValue(){ return ""; } }; },
  localStorage: { getItem(){ return null; }, setItem(){}, removeItem(){} },
};
global.localStorage = global.window.localStorage;
global.navigator = { clipboard: { writeText(){ return Promise.resolve(); } } };
global.location = { href: "http://127.0.0.1:8765/", reload(){} };
global.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
global.alert = () => {};

try {
  (0, eval)(script + "\n;globalThis.__smoke = { get lbMode(){ return lbMode; } };");
  console.log("LOAD OK");
} catch (e) {
  console.error("LOAD FAIL:", e.stack.split("\n").slice(0, 4).join("\n"));
  process.exit(1);
}
const getMode = () => globalThis.__smoke.lbMode;

// 模拟解析结果渲染（视频，DASH 双流）
const infoVideo = {
  title: "T", author: "A", cover: "http://x/c.jpg", duration: "0:08",
  platform: "twitter", webpage: "http://x/w", resolutions: ["360P", "480P", "720P"],
  views: 1, content_type: "video", image_count: 0, images: [],
  preview_url: "http://x/v.m4s", preview_silent: true, preview_audio_url: "http://x/a.m4a",
};
try {
  renderResult(infoVideo);
  console.log("RENDER video OK | playBtn shown:", document.getElementById("playBtn").style.display === "", "| previewBtn shown:", document.getElementById("previewBtn").style.display === "", "| coverBtn shown:", document.getElementById("coverBtn").style.display === "");
  openPreview();
  console.log("OPEN preview OK | lbMode:", getMode());
  closeLightbox();
  console.log("CLOSE OK");
} catch (e) {
  console.error("VIDEO PREVIEW FAIL:", e.stack.split("\n").slice(0, 4).join("\n"));
  process.exit(1);
}

// 音频
try {
  renderResult({ ...infoVideo, content_type: "audio", preview_url: "http://x/a.m4a", preview_silent: false, preview_audio_url: "", resolutions: [] });
  openPreview();
  console.log("AUDIO preview OK | lbMode:", getMode(), "| btn text:", document.getElementById("previewBtn").textContent);
  closeLightbox();
} catch (e) {
  console.error("AUDIO PREVIEW FAIL:", e.stack.split("\n").slice(0, 4).join("\n"));
  process.exit(1);
}

// 图集灯箱不受影响
try {
  renderResult({ ...infoVideo, content_type: "image", cover: "http://x/1.jpg", images: [{ url: "http://x/1.jpg" }, { url: "http://x/2.jpg" }], image_count: 2, preview_url: "" });
  openLightbox(["http://x/1.jpg", "http://x/2.jpg"], 0);
  console.log("IMAGE lightbox OK | lbMode:", getMode());
  closeLightbox();
} catch (e) {
  console.error("IMAGE FAIL:", e.stack.split("\n").slice(0, 4).join("\n"));
  process.exit(1);
}

// 无预览地址时按钮应隐藏
try {
  renderResult({ ...infoVideo, preview_url: "", preview_audio_url: "" });
  console.log("NO-PREVIEW OK | previewBtn hidden:", document.getElementById("previewBtn").style.display === "none", "| playBtn hidden:", document.getElementById("playBtn").style.display === "none");
} catch (e) {
  console.error("NO-PREVIEW FAIL:", e.stack.split("\n").slice(0, 4).join("\n"));
  process.exit(1);
}
console.log("SMOKE ALL PASS");
