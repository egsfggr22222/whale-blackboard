"""黑板（blackboard）：一块常驻桌面的无边框实时画布，由 agent 写代码、它即时呈现。

窗口规矩
--------
* 无边框（frameless）：没有标题栏、没有系统关闭按钮；
* 右上角一颗自绘「缩小」圆点：点一下缩成胶囊，再点一下还原；
* 用户关不掉它 —— 系统级关闭请求（Alt+F4 / 任务栏「关闭窗口」）会被
  `FormClosing` 拦截并取消；只有 `blackboard_close` 能真正关掉。

实时怎么来
--------
窗口加载的是本文件自己起的一个本地 HTTP 页面（127.0.0.1 随机端口 + 令牌）。
页面里一张 `<canvas>` + 一条 SSE 长连接：服务端一 publish，页面立刻收到并重绘。

工具
----
  blackboard_open      打开 / 唤起窗口
  blackboard_draw      推一张静态画面（ops）
  blackboard_eval      推一段 JS：定义了 frame(env) 就每帧跑（自写动画）
  blackboard_run       Python 侧逐帧推一组画面
  blackboard_status    看状态
  blackboard_minimize  缩成胶囊
  blackboard_restore   还原
  blackboard_close     关闭（唯一能关的入口）

帧协议（每个 dict 是一条绘图指令，坐标是 CSS 像素）
---------------------------------------------------
  {"op":"clear","bg":"#0b0e17"}
  {"op":"rect","x":..,"y":..,"w":..,"h":..,"color":"#fff","r":0,"fill":true,"alpha":1}
  {"op":"circle","x":..,"y":..,"r":..,"color":"..","fill":true,"width":2}
  {"op":"line","x1":..,"y1":..,"x2":..,"y2":..,"color":"..","width":2}
  {"op":"poly","points":[[x,y],..],"color":"..","close":true,"fill":false,"width":2}
  {"op":"path","d":"M0 0 L10 10","color":"..","width":2}
  {"op":"text","x":..,"y":..,"text":"..","size":28,"color":"#fff","align":"left","weight":""}
  {"op":"image","x":..,"y":..,"w":..,"h":..,"src":"data:image/png;base64,.."}
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import re
import signal
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

def _load_pytools_client():
    """找到 pytools_client 模块。

    插件通常会用 PYTHONPATH / PYTOOLS_CLIENT_DIR 把它的位置交过来；但某些启动方式
    （例如从桌面快捷方式起来的 GUI）里这两个变量到不了子进程，于是这里再兜底几种
    常见位置。找到之后就加到 sys.path 最前面。
    """
    try:
        import pytools_client  # noqa: F401
        return
    except ImportError:
        pass
    candidates = []
    for key in ("PYTOOLS_CLIENT_DIR", "PYTOOLS_HOME"):
        value = os.environ.get(key)
        if value:
            candidates.append(value)
    for part in (os.environ.get("PYTHONPATH") or "").split(os.pathsep):
        if part.strip():
            candidates.append(part.strip())
    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(here)
    candidates.extend(
        [
            os.path.join(parent, "dsh-python-tools"),
            os.path.join(os.path.expanduser("~"), ".dsh", "python-tools"),
            here,
        ]
    )
    for folder in candidates:
        if folder and os.path.isfile(os.path.join(folder, "pytools_client.py")):
            if folder not in sys.path:
                sys.path.insert(0, folder)
            return


_load_pytools_client()

import pytools_client as pytools
from pytools_client import register, skill
from pytools_client import main as pytools_main

pytools.describe(
    "黑板：无边框实时画布窗口。用代码在上面画图案 / 跑动画 / 做演示，用户实时看到。",
    tags=["blackboard", "teaching", "canvas", "live", "gui"],
)

STATE_DIR = os.path.join(os.path.expanduser("~"), ".dsh", "python-tools", "state", "blackboard")
SERVER_FILE = os.path.join(STATE_DIR, "server.json")
SCENE_FILE = os.path.join(STATE_DIR, "scene.json")
ACTION_FILE = os.path.join(STATE_DIR, "action.json")
DONE_FILE = os.path.join(STATE_DIR, "action.done.json")
LOG_FILE = os.path.join(STATE_DIR, "server.log")

PAGE_TITLE = "黑板 · Blackboard"
ICON_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blackboard.ico")
ASK_BAR_H = 62          # 底部问答栏高度（页面与窗口尺寸都要对齐）
DEFAULT_W, DEFAULT_H = 1120, 780
BG_DEFAULT = "#0b0e17"
ASK_FILE = os.path.join(STATE_DIR, "ask.jsonl")
LECTURE_FILE = os.path.join(STATE_DIR, "lecture.json")
# 归档：与 blackboard.py 放在一起的 history/ 目录
HISTORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history")
# 是否允许用户自己关窗：AI 停止思考时允许，思考/讲解中不允许
CLOSE_STATE: Dict[str, Any] = {"allowed": True}
# 子类化窗口时的回调对象必须常驻，否则会被 GC 回收导致崩溃
_KEEPALIVE: List[Any] = []




def _ensure_state_dir() -> str:
    # 走插件的通用能力：目录已存在就什么都不做
    return pytools.state_dir("blackboard")


def _atomic_write(path: str, text: str) -> None:
    # pytools.write_json 接受已序列化的字符串，所以调用方一行都不用改
    pytools.write_json(path, text)


def _read_json(path: str) -> Dict[str, Any]:
    data = pytools.read_json(path, {})
    return data if isinstance(data, dict) else {}


def _pid_alive(pid: Any) -> bool:
    return pytools.pid_alive(pid)


def _python() -> str:
    return sys.executable or "python"


def _detached_flags() -> Dict[str, Any]:
    if os.name == "nt":
        return {
            "creationflags": getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        }
    return {"start_new_session": True}


def _open_log() -> Any:
    _ensure_state_dir()
    return open(LOG_FILE, "a", encoding="utf-8")


def _http(method: str, path: str, payload: Optional[Dict[str, Any]] = None,
          timeout: float = 15.0) -> Dict[str, Any]:
    info = _read_json(SERVER_FILE)
    port, token = info.get("port"), info.get("token")
    if not port or not token:
        return {"ok": False, "error": "黑板还没启动"}
    import urllib.error
    import urllib.request

    data = None
    headers = {"X-Board-Token": str(token)}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(
        f"http://127.0.0.1:{int(port)}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {"ok": True}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        return {"ok": False, "error": f"HTTP {exc.code}: {detail}"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _server_alive() -> bool:
    info = _read_json(SERVER_FILE)
    if not info or not _pid_alive(info.get("pid")):
        return False
    return bool(_http("GET", "/api/ping", timeout=2.0).get("ok"))



HISTORY_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>历史</title>
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; background: #0b0e17; overflow: hidden;
    font-family: 'Segoe UI', 'Microsoft YaHei UI', 'Microsoft YaHei', system-ui, sans-serif;
    color: #e8eef8; user-select: none; }
  #top { height: 44px; display: flex; align-items: center; padding: 0 14px;
    border-bottom: 1px solid #1f2636; cursor: move; }
  #top .t { font-size: 15px; font-weight: 600; }
  #top .n { margin-left: 8px; font-size: 12px; color: #6272a4; }
  /* 无边框窗口：自己画一个关闭叉 */
  #close {
    margin-left: auto; width: 30px; height: 22px; border-radius: 4px;
    display: flex; align-items: center; justify-content: center;
    cursor: pointer; color: rgba(190,230,255,.8); font: 18px/1 Consolas, monospace;
  }
  #close:hover { background: rgba(255,90,90,.22); color: #ff8b7a; }
  #list { position: absolute; inset: 44px 0 58px 0; overflow-y: auto; padding: 10px 12px; }
  .row { display: flex; gap: 12px; align-items: center; padding: 8px; border-radius: 10px;
    cursor: pointer; border: 1px solid transparent; }
  .row:hover { background: #151c2b; border-color: #2a3348; }
  .thumb { flex: 0 0 160px; width: 160px; height: 100px; border-radius: 6px;
    background: #0f1420; border: 1px solid #232b3d; overflow: hidden; }
  .thumb canvas { width: 100%; height: 100%; display: block; }
  .meta { flex: 1 1 auto; min-width: 0; }
  .brief { font-size: 16px; font-weight: 600; margin-bottom: 6px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sub { font-size: 12px; color: #7f8aa3; }
  #empty { padding: 40px 20px; text-align: center; color: #5b657a; font-size: 14px; }
  #bottom { position: absolute; left: 0; right: 0; bottom: 0; height: 58px;
    display: flex; align-items: center; justify-content: flex-end; gap: 10px;
    padding: 0 14px; border-top: 1px solid #1f2636; background: #10141f; }
  #clear { height: 34px; padding: 0 16px; border-radius: 8px; border: 1px solid #5a2a2a;
    background: #2a1418; color: #ff8b7a; cursor: pointer; font-size: 13px; }
  #clear:hover { background: #3a1a20; }
  #ctx { position: absolute; z-index: 50; min-width: 120px; display: none;
    background: #131a28; border: 1px solid #2a3348; border-radius: 8px;
    box-shadow: 0 8px 24px rgba(0,0,0,.5); padding: 5px; }
  #ctx.open { display: block; }
  #ctx .item { padding: 8px 12px; border-radius: 6px; font-size: 14px; cursor: pointer;
    color: #ff8b7a; }
  #ctx .item:hover { background: #2a1418; }
</style>
</head>
<body>
<div id="top"><span class="t">历史</span><span class="n" id="count"></span><span id="close" title="关闭">✕</span></div>
<div id="list"></div>
<div id="bottom"><button id="clear" type="button">清空</button></div>
<div id="ctx"><div class="item" id="ctx-del">删除</div></div>
<script>
(function () {
  "use strict";
  var TOKEN = "__TOKEN__";
  var list = document.getElementById("list");
  var countEl = document.getElementById("count");
  var ctx = document.getElementById("ctx");
  var ctxDel = document.getElementById("ctx-del");
  var ctxTarget = null;

  function api(path, body) {
    var opt = { method: body === undefined ? "GET" : "POST" };
    if (body !== undefined) {
      opt.headers = { "Content-Type": "application/json" };
      opt.body = JSON.stringify(body);
    }
    return fetch(path + (path.indexOf("?") < 0 ? "?" : "&") +
                 "token=" + encodeURIComponent(TOKEN), opt).then(function (r) { return r.json(); });
  }

  /* 用归档里的 ops 在缩略图 canvas 上重绘首屏 */
  function paint(canvas, item) {
    var dpr = window.devicePixelRatio || 1;
    var w = 160, h = 100;
    canvas.width = w * dpr; canvas.height = h * dpr;
    var ctx2 = canvas.getContext("2d");
    ctx2.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx2.fillStyle = item.thumbBg || "#0b0e17";
    ctx2.fillRect(0, 0, w, h);
    var ops = item.thumb || [];
    // 估算原画布尺寸：取 ops 里的最大坐标，缩到 160x100
    var maxX = 800, maxY = 600;
    ops.forEach(function (o) {
      if (o.op === "rect" || o.op === "image") {
        maxX = Math.max(maxX, (o.x || 0) + (o.w || 0));
        maxY = Math.max(maxY, (o.y || 0) + (o.h || 0));
      } else if (o.op === "text") {
        maxX = Math.max(maxX, (o.x || 0) + String(o.text || "").length * (o.size || 20) * 0.6);
        maxY = Math.max(maxY, (o.y || 0) + (o.size || 20));
      } else if (o.op === "circle") {
        maxX = Math.max(maxX, (o.x || 0) + (o.r || 0));
        maxY = Math.max(maxY, (o.y || 0) + (o.r || 0));
      }
    });
    var sx = w / maxX, sy = h / maxY;
    var s = Math.min(sx, sy);
    ctx2.save();
    ctx2.scale(s, s);
    ops.forEach(function (o) {
      try {
        ctx2.globalAlpha = (typeof o.alpha === "number") ? o.alpha : 1;
        ctx2.strokeStyle = o.color || "#8be9fd";
        ctx2.fillStyle = o.color || "#8be9fd";
        ctx2.lineWidth = o.width || 2;
        ctx2.font = (o.size || 20) + "px sans-serif";
        ctx2.textAlign = o.align || "left";
        if (o.op === "rect") {
          if (o.fill === false) { ctx2.strokeRect(o.x, o.y, o.w, o.h); }
          else { ctx2.fillRect(o.x, o.y, o.w, o.h); }
        } else if (o.op === "circle") {
          ctx2.beginPath(); ctx2.arc(o.x, o.y, Math.max(0, o.r || 0), 0, Math.PI * 2);
          if (o.fill === false) ctx2.stroke(); else ctx2.fill();
        } else if (o.op === "line") {
          ctx2.beginPath(); ctx2.moveTo(o.x1, o.y1); ctx2.lineTo(o.x2, o.y2); ctx2.stroke();
        } else if (o.op === "text") {
          ctx2.fillText(String(o.text == null ? "" : o.text), o.x, o.y);
        }
      } catch (e) {}
      ctx2.globalAlpha = 1;
    });
    ctx2.restore();
  }

  function when(ts) {
    if (!ts) return "";
    var d = new Date(ts * 1000);
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
           " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function render(items) {
    list.innerHTML = "";
    countEl.textContent = items.length ? "共 " + items.length + " 条" : "";
    if (!items.length) {
      var empty = document.createElement("div");
      empty.id = "empty";
      empty.textContent = "还没有历史记录。";
      list.appendChild(empty);
      return;
    }
    items.forEach(function (item) {
      var row = document.createElement("div");
      row.className = "row";
      row.dataset.name = item.name;

      var thumbBox = document.createElement("div");
      thumbBox.className = "thumb";
      var canvas = document.createElement("canvas");
      thumbBox.appendChild(canvas);
      row.appendChild(thumbBox);
      paint(canvas, item);

      var meta = document.createElement("div");
      meta.className = "meta";
      var brief = document.createElement("div");
      brief.className = "brief";
      brief.textContent = item.brief || item.topic || "未命名";
      var sub = document.createElement("div");
      sub.className = "sub";
      sub.textContent = item.screens + " 屏 · " + (item.completed ? "已完成" : "未完成") +
                        (item.endedAt ? " · " + when(item.endedAt) : "");
      meta.appendChild(brief);
      meta.appendChild(sub);
      row.appendChild(meta);

      // 双击：载入（写队列，由 agent 重放）。单击不触发。
      row.addEventListener("dblclick", function () {
        api("/api/history-load", { name: item.name }).catch(function () {});
        row.style.borderColor = "#4a7fd4";
      });
      // 右键：弹出删除菜单
      row.addEventListener("contextmenu", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        ctxTarget = item.name;
        ctx.style.left = Math.min(ev.clientX, window.innerWidth - 140) + "px";
        ctx.style.top = Math.min(ev.clientY, window.innerHeight - 60) + "px";
        ctx.classList.add("open");
      });

      list.appendChild(row);
    });
  }

  function refresh() {
    api("/api/history-list").then(function (res) {
      render((res && res.items) || []);
    }).catch(function () { render([]); });
  }

  document.addEventListener("click", function () { ctx.classList.remove("open"); });
  ctxDel.addEventListener("click", function (ev) {
    ev.stopPropagation();
    ctx.classList.remove("open");
    if (!ctxTarget) return;
    api("/api/history-delete", { name: ctxTarget }).then(refresh).catch(function () {});
    ctxTarget = null;
  });

  document.getElementById("clear").addEventListener("click", function () {
    api("/api/history-clear", {}).then(refresh).catch(function () {});
  });

  /* 无边框窗口：自己画关闭叉 + 顶栏拖动；列表区不拖窗 */
  document.getElementById("close").addEventListener("click", function (ev) {
    ev.stopPropagation();
    // 不能用 window.close()：那是浏览器 API，pywebview 的原生窗口不认，
    // 而且不会报错 —— 表现就是“点了没反应”。改成通知 Host 去销毁窗口。
    fetch("/api/history-window-close?token=" + encodeURIComponent(TOKEN), { method: "POST" })
      .catch(function () {});
  });
  var topBar = document.getElementById("top");
  topBar.classList.add("pywebview-drag-region");
  // 叉叉自己不能被拖动区吞掉
  ["mousedown", "pointerdown"].forEach(function (name) {
    document.getElementById("close").addEventListener(name, function (ev) {
      ev.stopPropagation();
    }, true);
  });
  // 列表和底部按钮不参与拖窗
  [list, document.getElementById("bottom"), ctx].forEach(function (el) {
    if (!el) return;
    ["mousedown", "pointerdown"].forEach(function (name) {
      el.addEventListener(name, function (ev) { ev.stopPropagation(); }, true);
    });
  });

  refresh();
})();
</script>
</body>
</html>
"""


PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; height: 100%; overflow: hidden; background: __BG__;
               font-family: 'Segoe UI', 'Microsoft YaHei UI', 'Microsoft YaHei', system-ui, sans-serif; }
  /* 上面是画布，下面是问答区（固定高） */
  #board { position: absolute; inset: 0 0 __ASK_H__px 0; overflow: hidden; }
  #stage { position: absolute; inset: 0; width: 100%; height: 100%; display: block;
           transform-origin: 0 0; transition: transform .08s linear; }
  /* 右上角：只有一条横线，点击最小化到任务栏 */
  #dot {
    position: absolute; top: 8px; right: 12px; width: 34px; height: 22px;
    display: flex; align-items: center; justify-content: center;
    cursor: pointer; user-select: none; z-index: 20; border-radius: 4px;
    transition: background .14s ease;
  }
  #dot:hover { background: rgba(120,210,255,.16); }
  #dot span { display: block; width: 14px; height: 2px; background: rgba(190,230,255,.85); border-radius: 1px; }
  /* 当前缩放比例，Ctrl+滚轮时能看到 */
  #zoom {
    position: absolute; top: 8px; right: 96px; height: 22px; padding: 0 7px;
    display: flex; align-items: center; border-radius: 4px;
    font: 11px/1 Consolas, monospace; color: rgba(190,230,255,.9);
    background: rgba(120,210,255,.08); user-select: none; pointer-events: none;
    transition: opacity .2s ease;
  }
  /* 右上角「...」：在横线左边，点开是下拉菜单 */
  #more {
    position: absolute; top: 8px; right: 52px; width: 34px; height: 22px;
    display: flex; align-items: center; justify-content: center;
    cursor: pointer; user-select: none; z-index: 21; border-radius: 4px;
    transition: background .14s ease;
    font: 700 16px/1 Consolas, monospace; letter-spacing: 1px; color: rgba(190,230,255,.85);
  }
  #more:hover { background: rgba(120,210,255,.16); }
  #menu {
    position: absolute; top: 34px; right: 52px; min-width: 132px; z-index: 40;
    background: #131a28; border: 1px solid #2a3348; border-radius: 8px;
    box-shadow: 0 8px 24px rgba(0,0,0,.45); padding: 6px; display: none;
  }
  #menu.open { display: block; }
  #menu .item {
    padding: 8px 12px; border-radius: 6px; cursor: pointer;
    font-size: 14px; color: #e8eef8; user-select: none;
  }
  #menu .item:hover { background: #1f2a3d; }
  #ask {
    position: absolute; left: 0; right: 0; bottom: 0; height: __ASK_H__px; z-index: 30;
    background: #10141f; border-top: 1px solid #1f2636;
    display: flex; align-items: center; gap: 10px; padding: 0 14px;
  }
  /* 拖动区分不在输入栏，它自然不参与拖动；整个画面区域加 pywebview 的拖动类名 */
  #board {
    position: absolute; inset: 0 0 __ASK_H__px 0;
    cursor: move;
  }
  #ask input {
    flex: 1 1 auto; height: 36px; padding: 0 14px; border-radius: 8px;
    background: #0b0e17; color: #e8eef8; border: 1px solid #2a3348; outline: none;
    font-size: 14px; font-family: inherit;
  }
  #ask input:focus { border-color: #4a7fd4; }
  #ask input::placeholder { color: #5b657a; }
  #send {
    flex: 0 0 auto; height: 36px; padding: 0 18px; border-radius: 8px; cursor: pointer;
    background: #1f6feb; color: #fff; border: none; font-size: 14px; font-family: inherit;
    user-select: none; transition: background .14s ease, transform .1s ease;
  }
  #send:hover { background: #2d7ff9; }
  #send:active { transform: scale(.97); }
  #send.hint { background: #2a3348; color: #9fb0c8; }
  #badge {
    position: absolute; left: 14px; bottom: __ASK_H__px; transform: translateY(-6px); z-index: 20;
    pointer-events: none; font: 11px/1.2 Consolas, monospace;
    color: rgba(150,180,210,.45); letter-spacing: .08em;
  }
</style>
</head>
<body>
<div id="board" class="pywebview-drag-region"><canvas id="stage"></canvas></div>
<div id="zoom">100%</div>
<div id="dot" title="最小化到任务栏"><span></span></div>
<div id="more" title="更多">···</div>
<div id="menu"><div class="item" id="menu-history">历史</div></div>
<div id="badge">BLACKBOARD</div>
<div id="ask">
  <input id="ask-input" type="text" placeholder="在这里输入你的问题，点击右侧发送…" autocomplete="off" spellcheck="false">
  <button id="send" type="button" title="点击发送">发送</button>
</div>
<script>
(function () {
  "use strict";
  var TOKEN = "__TOKEN__";
  var BG = "__BG__";
  var canvas = document.getElementById("stage");
  var ctx = canvas.getContext("2d");
  var W = 0, H = 0;
  // 缩放比例：只影响显示，不改画布像素尺寸，所以坐标系统完全不变
  var ZOOM = 1.0;
  var ZOOM_MIN = 0.5, ZOOM_MAX = 2.5;

  function applyZoom(next) {
    ZOOM = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, next));
    canvas.style.transform = "scale(" + ZOOM + ")";
    var badge = document.getElementById("zoom");
    if (badge) {
      badge.textContent = Math.round(ZOOM * 100) + "%";
      badge.style.opacity = ZOOM === 1 ? "0.35" : "1";
    }
  }

  function setZoom(percent) {
    var value = Number(percent);
    if (!isFinite(value) || value <= 0) value = 100;
    applyZoom(value / 100);
  }

  function fitSize() {
    var dpr = window.devicePixelRatio || 1;
    // 画布只占上面那块 #board：底部问答区不参与绘制
    var box = document.getElementById("board").getBoundingClientRect();
    W = Math.max(1, Math.round(box.width));
    H = Math.max(1, Math.round(box.height));
    canvas.width = Math.max(1, Math.round(W * dpr));
    canvas.height = Math.max(1, Math.round(H * dpr));
    canvas.style.width = W + "px";
    canvas.style.height = H + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    repaint();
  }

  /* ── 绘图 ──────────────────────────────────────────────────── */
  var IMG = {};

  function style(o) {
    ctx.globalAlpha = (typeof o.alpha === "number") ? Math.max(0, Math.min(1, o.alpha)) : 1;
    var color = o.color || "#8be9fd";
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = (typeof o.width === "number") ? o.width : 2;
    ctx.lineJoin = "round"; ctx.lineCap = "round";
    ctx.font = (o.weight ? o.weight + " " : "") + (o.size || 28) +
               "px 'Segoe UI', 'Microsoft YaHei UI', 'Microsoft YaHei', system-ui, sans-serif";
    ctx.textAlign = o.align || "left";
    ctx.textBaseline = "alphabetic";
  }

  function roundRect(x, y, w, h, r) {
    if (!r) { ctx.beginPath(); ctx.rect(x, y, w, h); return; }
    var rr = Math.min(Math.abs(r), Math.abs(w) / 2, Math.abs(h) / 2);
    ctx.beginPath();
    ctx.moveTo(x + rr, y);
    ctx.arcTo(x + w, y, x + w, y + h, rr);
    ctx.arcTo(x + w, y + h, x, y + h, rr);
    ctx.arcTo(x, y + h, x, y, rr);
    ctx.arcTo(x, y, x + w, y, rr);
    ctx.closePath();
  }

  function drawOps(ops) {
    for (var i = 0; i < ops.length; i++) {
      var o = ops[i];
      if (!o || !o.op) continue;
      if (o.op === "image") { drawImage(o); continue; }
      style(o);
      if (o.op === "clear") {
        ctx.globalAlpha = 1; ctx.fillStyle = o.bg || BG;
        ctx.fillRect(0, 0, W, H);
      } else if (o.op === "rect") {
        roundRect(o.x || 0, o.y || 0, o.w || 0, o.h || 0, o.r || 0);
        if (o.fill === false) ctx.stroke(); else ctx.fill();
      } else if (o.op === "circle") {
        var r = Math.max(0, o.r || 0);
        ctx.beginPath(); ctx.arc(o.x || 0, o.y || 0, r, 0, Math.PI * 2);
        if (o.fill === false) ctx.stroke(); else ctx.fill();
      } else if (o.op === "line") {
        ctx.beginPath(); ctx.moveTo(o.x1 || 0, o.y1 || 0);
        ctx.lineTo(o.x2 || 0, o.y2 || 0); ctx.stroke();
      } else if (o.op === "poly") {
        var pts = o.points || [];
        if (pts.length < 2) continue;
        ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]);
        for (var k = 1; k < pts.length; k++) ctx.lineTo(pts[k][0], pts[k][1]);
        if (o.close) ctx.closePath();
        if (o.fill) ctx.fill(); else ctx.stroke();
      } else if (o.op === "path") {
        try { ctx.stroke(new Path2D(o.d || "")); } catch (e) {}
      } else if (o.op === "text") {
        ctx.fillText(String(o.text == null ? "" : o.text), o.x || 0, o.y || 0);
      }
      ctx.globalAlpha = 1;
    }
  }

  function drawImage(o) {
    var src = o.src || "";
    if (!src) return;
    var got = IMG[src];
    if (!got) {
      got = new Image();
      got.onload = repaint;
      got.src = src;
      IMG[src] = got;
      return;
    }
    if (!got.complete || !got.naturalWidth) return;
    ctx.globalAlpha = (typeof o.alpha === "number") ? o.alpha : 1;
    ctx.drawImage(got, o.x || 0, o.y || 0, o.w || got.naturalWidth, o.h || got.naturalHeight);
    ctx.globalAlpha = 1;
  }

  /* ── 两条渲染路径：静态场景 / 自包含动画 ─────────────────────── */
  var mode = "idle";      // idle | static | anim
  var staticOps = [];
  var frameFn = null;
  var animStart = 0;
  var running = false;
  var lastFrame = [];

  function setStatic(ops, bg) {
    mode = "static";
    staticOps = [{ op: "clear", bg: bg || BG }].concat(ops || []);
    repaint();
  }

  function setAnim(js, bg) {
    frameFn = null; staticOps = [];
    if (!js) { setStatic([], bg); return { ok: true, mode: "static" }; }
    var fn;
    try {
      fn = new Function("env", js + "\n;return (typeof frame === 'function') ? frame : null;")(null);
    } catch (e) {
      setStatic([{ op: "text", x: 28, y: 64, text: "代码出错：" + e.message,
                   size: 20, color: "#ff6b81" }], bg);
      return { ok: false, error: String(e) };
    }
    if (typeof fn !== "function") {
      setStatic(lastFrame, bg);
      return { ok: true, mode: "once" };
    }
    frameFn = fn;
    mode = "anim";
    animStart = performance.now();
    if (!running) { running = true; window.requestAnimationFrame(tick); }
    return { ok: true, mode: "anim" };
  }

  function tick(now) {
    if (mode !== "anim" || !frameFn) { running = false; return; }
    window.requestAnimationFrame(tick);
    var t = (now - animStart) / 1000;
    var collected = [];
    var env = {
      t: t, w: W, h: H,
      draw: function (ops) { if (ops) for (var i = 0; i < ops.length; i++) collected.push(ops[i]); }
    };
    try {
      var out = frameFn(env);
      if (out && out.length) collected = out;
    } catch (e) {
      frameFn = null; mode = "static";
      collected = [{ op: "clear", bg: BG },
                   { op: "text", x: 28, y: 64, text: "动画出错：" + e.message, size: 20, color: "#ff6b81" }];
    }
    lastFrame = collected;
    ctx.clearRect(0, 0, W, H);
    drawOps(collected);
  }

  function repaint() {
    if (mode === "anim") return;
    ctx.clearRect(0, 0, W, H);
    drawOps(staticOps.length ? staticOps : [{ op: "clear", bg: BG }]);
  }

  /* agent 可以在 eval 里直接用的入口 */
  window.__board = {
    setOps: setStatic,
    anim: setAnim,
    clear: function (bg) { setStatic([], bg); },
    ctx: function () { return ctx; },
    canvas: function () { return canvas; },
    size: function () { return { w: W, h: H, dpr: window.devicePixelRatio || 1 }; },
    repaint: repaint
  };

  window.addEventListener("resize", fitSize);
  fitSize();

  /* ── SSE：服务端一推，这里立刻换画面 ─────────────────────────── */
  var badge = document.getElementById("badge");
  var source = new EventSource("/api/stream?token=" + encodeURIComponent(TOKEN));

  /* 画完一帧就告诉服务端「第 seq 帧已经上屏了」。
     服务端靠这个区分「帧推出去了」和「帧真的被画出来了」——
     窗口静默死掉时前者会涨、后者不动。 */
  function ack(seq) {
    if (!seq) return;
    // 下一帧再报，避开和绘制抢主线程
    setTimeout(function () {
      fetch("/api/ack?token=" + encodeURIComponent(TOKEN) + "&seq=" + seq)
        .catch(function () {});
    }, 0);
  }

  function apply(msg) {
    // 缩放指令：只改显示比例，不碰画布内容
    if (msg && msg.zoom) { setZoom(msg.zoom); ack(msg.seq); return; }
    if (msg && msg.js) { setAnim(msg.js, msg.bg); ack(msg.seq); return; }
    setStatic((msg && msg.ops) || [], msg && msg.bg);
    ack(msg && msg.seq);
  }

  source.addEventListener("hello", function (ev) {
    badge.textContent = "BLACKBOARD · LIVE";
    // 新连接一律从 100% 开始：缩放是“当场临时状态”，不该跨窗口继承。
    // 以前缩放指令会写进服务端的 last 帧，重连时被重放，
    // 结果新窗口一开就卡在旧比例（实测被留在 50%，画面只占左上角一块）。
    applyZoom(1.0);
    try { apply(JSON.parse(ev.data)); } catch (e) {}
  });
  source.addEventListener("message", function (ev) {
    try { apply(JSON.parse(ev.data)); } catch (e) {}
  });
  source.onerror = function () { badge.textContent = "重连中…"; };
  source.onopen = function () { badge.textContent = "BLACKBOARD · LIVE"; };

  /* 首帧兜底：SSE 建立前先抓一次当前场景 */
  fetch("/api/scene?token=" + encodeURIComponent(TOKEN))
    .then(function (r) { return r.json(); })
    .then(function (scene) { if (scene && (scene.ops || scene.js)) apply(scene); })
    .catch(function () {});

  /* ── Ctrl + 滚轮缩放 ─────────────────────────────────────
     easy_drag 会把鼠标事件提前吃掉，WebView2 自带的 Ctrl+滚轮缩放收不到，
     所以这里自己接管：外层容器 transform: scale，不改画布像素，
     坐标系统不变，帧协议也不用改。 */
  window.addEventListener("wheel", function (ev) {
    if (!ev.ctrlKey) return;          // 不按 Ctrl 就不管
    ev.preventDefault();
    ev.stopPropagation();
    applyZoom(ZOOM - ev.deltaY * 0.0012);
  }, { passive: false });

  /* Ctrl+加号/减号/Ctrl+0 也接到同一套里，避免两套缩放打架 */
  window.addEventListener("keydown", function (ev) {
    if (!ev.ctrlKey) return;
    if (ev.key === "+" || ev.key === "=") { ev.preventDefault(); applyZoom(ZOOM + 0.1); }
    else if (ev.key === "-" || ev.key === "_") { ev.preventDefault(); applyZoom(ZOOM - 0.1); }
    else if (ev.key === "0") { ev.preventDefault(); applyZoom(1.0); }
  }, true);

  /* ── 右上角横线：最小化到任务栏 ─────────────────────────────── */
  var dot = document.getElementById("dot");
  dot.addEventListener("click", function (ev) {
    ev.stopPropagation(); ev.preventDefault();
    fetch("/api/minimize?token=" + encodeURIComponent(TOKEN), { method: "POST" })
      .catch(function () {});
  });

  /* ── 底部问答：输入后双击发送 ───────────────────────────────── */
  var input = document.getElementById("ask-input");
  var sendBtn = document.getElementById("send");
  var sending = false;

  function sendQuestion() {
    var text = (input.value || "").trim();
    if (text === "" || sending) return;
    sending = true;
    sendBtn.textContent = "已发送";
    sendBtn.classList.add("hint");
    fetch("/api/ask?token=" + encodeURIComponent(TOKEN), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text })
    }).then(function (r) { return r.json(); }).then(function (res) {
      if (res && res.ok) {
        input.value = "";
      }
    }).catch(function () {}).then(function () {
      setTimeout(function () {
        sending = false;
        sendBtn.textContent = "发送";
        sendBtn.classList.remove("hint");
      }, 900);
    });
  }

  // 严格按用户要求：只能点发送按钮（双击）才发；回车不发，也不弹提示
  sendBtn.addEventListener("click", function (ev) {
    ev.preventDefault();
    sendQuestion();
  });
  input.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") { ev.preventDefault(); }   // 回车只管换行/无事发生
  });

  /* ── 底部输入区不参与拖窗 ─────────────────────────────────────
     pywebview 的 easy_drag 是挂在 window 上的：任何位置按下都会开始拖窗口。
     在捕获阶段就把输入区内的事件截住，不让它们传到 window。 */
  var askBar = document.getElementById("ask");
  ["mousedown", "mousemove", "mouseup", "pointerdown", "pointerup"].forEach(function (name) {
    askBar.addEventListener(name, function (ev) { ev.stopPropagation(); }, true);
  });

  /* ── 右上角「...」下拉菜单 ─────────────────────────────────── */
  var moreBtn = document.getElementById("more");
  var menu = document.getElementById("menu");
  var menuHistory = document.getElementById("menu-history");

  function closeMenu() { menu.classList.remove("open"); }

  moreBtn.addEventListener("click", function (ev) {
    ev.stopPropagation(); ev.preventDefault();
    menu.classList.toggle("open");
  });
  // 点其他地方关掉菜单
  document.addEventListener("click", function () { closeMenu(); });
  menu.addEventListener("click", function (ev) { ev.stopPropagation(); });

  menuHistory.addEventListener("click", function (ev) {
    ev.stopPropagation();
    closeMenu();
    // 打开独立的「历史」窗口（不再直接写队列；载入与否由用户在窗口里选）
    fetch("/api/history-window?token=" + encodeURIComponent(TOKEN), { method: "POST" })
      .catch(function () {});
  });

  // 这两个按钮也不能拖动窗口
  ["mousedown", "pointerdown"].forEach(function (name) {
    moreBtn.addEventListener(name, function (ev) { ev.stopPropagation(); }, true);
    menu.addEventListener(name, function (ev) { ev.stopPropagation(); }, true);
  });
})();
</script>
</body>
</html>
"""


def _page(token: str) -> str:
    return (PAGE_HTML.replace("__TITLE__", PAGE_TITLE)
            .replace("__TOKEN__", token)
            .replace("__BG__", BG_DEFAULT)
            .replace("__ASK_H__", str(ASK_BAR_H)))




class _Bus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: List[Any] = []
        self.seq = 0
        # 页面画完一帧后回写的帧号。seq 涨了但这个不跟，就说明窗口没在真的画。
        self.rendered_seq = 0
        self.rendered_at = 0.0
        self.last: Dict[str, Any] = {"ops": [], "js": "", "bg": BG_DEFAULT, "seq": 0}

    def ack(self, seq: int) -> None:
        """页面报「第 seq 帧已经画到屏幕上了」。"""
        with self._lock:
            if seq >= self.rendered_seq:
                self.rendered_seq = int(seq)
                self.rendered_at = time.time()

    def add(self, channel: Any) -> None:
        with self._lock:
            self._clients.append(channel)

    def remove(self, channel: Any) -> None:
        with self._lock:
            if channel in self._clients:
                self._clients.remove(channel)

    def publish(self, payload: Dict[str, Any]) -> int:
        with self._lock:
            self.seq += 1
            message = dict(payload)
            message["seq"] = self.seq
            self.last = message
            for channel in list(self._clients):
                try:
                    channel.put_nowait(message)
                except Exception:
                    pass
            return self.seq

    def broadcast(self, payload: Dict[str, Any]) -> None:
        """只发给当前连着的页面，不记住、不重放。

        用于「只影响当场显示」的指令（比如缩放）：
        新窗口不应该继承上一个窗口的临时状态。
        """
        with self._lock:
            message = dict(payload)
            for channel in list(self._clients):
                try:
                    channel.put_nowait(message)
                except Exception:
                    pass


BUS = _Bus()
TOKEN = ""
# 已废弃的胶囊状态：保留常量仅为让旧调用不报错，行为已改为最小化到任务栏。
CAPSULE_STATE = {"capsule": False}

# 本次讲解的每一屏画面（归档时写入 history/）。只保存在服务进程内存里，
# 不落盘到 scene.json——这样下一次开黑板是干净的。
SCREENS: List[Dict[str, Any]] = []
SCREENS_LOCK = threading.Lock()


def _record_screen(payload: Dict[str, Any]) -> None:
    """记一屏。静态画面存 ops，动画存 js。"""
    screen = {
        "title": payload.get("title") or "",
        "at": time.time(),
    }
    if payload.get("js"):
        screen["js"] = payload.get("js")
    else:
        screen["ops"] = payload.get("ops") or []
    screen["bg"] = payload.get("bg") or BG_DEFAULT
    with SCREENS_LOCK:
        SCREENS.append(screen)


def _make_handler():
    class Handler(BaseHTTPRequestHandler):
        server_version = "Blackboard/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def _query(self) -> Dict[str, List[str]]:
            from urllib.parse import parse_qs, urlparse

            return parse_qs(urlparse(self.path).query)

        def _token_ok(self) -> bool:
            supplied = (self._query().get("token") or [self.headers.get("X-Board-Token", "")])[0]
            return supplied == TOKEN

        def _send_json(self, obj: Any, status: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            from urllib.parse import urlparse

            path = urlparse(self.path).path
            if path in ("/", "/board.html", "/index.html"):
                body = _page(TOKEN).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path in ("/history", "/history.html"):
                body = HISTORY_HTML.replace("__TOKEN__", TOKEN).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/history-list":
                if not self._token_ok():
                    self._send_json({"ok": False, "error": "bad token"}, 403)
                    return
                items = _history_records()
                self._send_json({"ok": True, "count": len(items), "items": items})
                return
            if path == "/api/ping":
                # 窗口进程与当前服务进程不是一个，必须按 windowPid 去找它的窗口。
                # windowPid 是窗口进程启动后写回 server.json 的，所以每次现读。
                win_pid = int(_read_json(SERVER_FILE).get("windowPid") or 0)
                self._send_json({"ok": True, "pid": os.getpid(), "seq": BUS.seq,
                                 "capsule": CAPSULE_STATE["capsule"],
                                 "renderedSeq": BUS.rendered_seq,
                                 "window": _window_health(win_pid),
                                 "closeAllowed": bool(CLOSE_STATE.get("allowed", True))})
                return
            if path == "/api/ack":
                # 页面每个帧画完后报一句：告诉服务端它真的画到了哪一帧
                try:
                    seq = int(self._query().get("seq", ["0"])[0])
                except (TypeError, ValueError):
                    seq = 0
                if seq > 0:
                    BUS.ack(seq)
                self._send_json({"ok": True, "seq": BUS.rendered_seq})
                return
            if not self._token_ok():
                self._send_json({"ok": False, "error": "bad token"}, 403)
                return
            if path == "/api/archive":
                # GET：服务端把本次讲解的全部画面打包交出来（归档用）
                lecture = _read_json(LECTURE_FILE)
                with SCREENS_LOCK:
                    screens = list(SCREENS)
                questions = []
                try:
                    with open(ASK_FILE, "r", encoding="utf-8") as handle:
                        for line in handle:
                            line = line.strip()
                            if line:
                                try:
                                    questions.append(json.loads(line))
                                except ValueError:
                                    pass
                except OSError:
                    pass
                self._send_json({"ok": True, "lecture": lecture,
                                 "screens": screens, "questions": questions})
                return
            if path == "/api/scene":
                self._send_json(BUS.last)
                return
            if path == "/api/stream":
                self._stream()
                return
            self._send_json({"ok": False, "error": "not found"}, 404)

        def _stream(self) -> None:
            import queue

            channel: Any = queue.Queue(maxsize=64)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            BUS.add(channel)
            try:
                hello = json.dumps(BUS.last, ensure_ascii=False)
                self.wfile.write(f"event: hello\ndata: {hello}\n\n".encode("utf-8"))
                self.wfile.flush()
                while True:
                    try:
                        message = channel.get(timeout=15)
                        data = json.dumps(message, ensure_ascii=False)
                        self.wfile.write(f"event: message\ndata: {data}\n\n".encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            finally:
                BUS.remove(channel)
                self.close_connection = True

        def do_POST(self):  # noqa: N802
            from urllib.parse import urlparse

            path = urlparse(self.path).path
            if not self._token_ok():
                self._send_json({"ok": False, "error": "bad token"}, 403)
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                self._send_json({"ok": False, "error": "bad json"}, 400)
                return

            if path == "/api/draw":
                message = {"ops": body.get("ops") or [], "js": "",
                           "bg": body.get("bg") or BG_DEFAULT,
                           "title": body.get("title") or ""}
                _record_screen(message)
                seq = BUS.publish(message)
                self._send_json({"ok": True, "seq": seq, "canvas": _canvas_info()})
                return
            if path == "/api/zoom":
                # agent 也能设缩放：body = {"percent": 80}
                #
                # 注意：缩放不能走 BUS.publish。publish 会把消息记成「最后一帧」，
                # 而窗口重连时 /api/scene 会把那一帧重放 —— 于是新窗口一启动
                # 就带着旧的缩放比例（实测被留在 50%，画面只占左上角一小块）。
                # 所以只往当前连着的客户端发，不写 last。
                try:
                    percent = float(body.get("percent") or 100)
                except (TypeError, ValueError):
                    percent = 100.0
                message = {"zoom": percent, "ops": [], "js": "",
                           "bg": BG_DEFAULT, "title": "zoom"}
                BUS.broadcast(message)
                self._send_json({"ok": True, "percent": percent})
                return
            if path == "/api/anim":
                message = {"ops": [], "js": body.get("js") or "",
                           "bg": body.get("bg") or BG_DEFAULT,
                           "title": body.get("title") or ""}
                _record_screen(message)
                seq = BUS.publish(message)
                self._send_json({"ok": True, "seq": seq})
                return
            if path == "/api/minimize":
                # 点右上角横线：最小化到任务栏（不是缩成小胶囊）
                _atomic_write(ACTION_FILE, json.dumps({"action": "minimize", "at": time.time()},
                                                     ensure_ascii=False))
                self._send_json({"ok": True})
                return
            if path == "/api/history-window-close":
                # 历史窗口的 ✕：写一个关闭请求，窗口进程读到就销毁窗口
                _atomic_write(HISTORY_CLOSE_FILE, json.dumps({"at": time.time()},
                                                           ensure_ascii=False))
                self._send_json({"ok": True})
                return
            if path == "/api/history-window":
                server_port = int(self.server.server_address[1])
                ok = _spawn_history_window(server_port)
                self._send_json({"ok": ok})
                return
            if path == "/api/history-delete":
                target = os.path.basename(str(body.get("name") or ""))
                if not target:
                    self._send_json({"ok": False, "error": "missing name"}, 400)
                    return
                try:
                    os.remove(os.path.join(HISTORY_DIR, target))
                except OSError as exc:
                    self._send_json({"ok": False, "error": str(exc)}, 200)
                    return
                self._send_json({"ok": True, "deleted": target})
                return
            if path == "/api/history-clear":
                removed = 0
                if os.path.isdir(HISTORY_DIR):
                    for filename in os.listdir(HISTORY_DIR):
                        if filename.endswith(".json"):
                            try:
                                os.remove(os.path.join(HISTORY_DIR, filename))
                                removed += 1
                            except OSError:
                                pass
                self._send_json({"ok": True, "removed": removed})
                return
            if path == "/api/history-load":
                # 用户左键单击某条：写入队列，由 agent 取到后重放到黑板
                target = os.path.basename(str(body.get("name") or ""))
                if not target:
                    self._send_json({"ok": False, "error": "missing name"}, 400)
                    return
                item = {"id": "load_%s" % uuid.uuid4().hex[:10],
                        "kind": "history-load", "name": target,
                        "text": "载入历史：%s" % target, "at": time.time()}
                os.makedirs(STATE_DIR, exist_ok=True)
                with open(ASK_FILE, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                self._send_json({"ok": True, "id": item["id"], "name": target})
                return
            if path == "/api/history":
                # 用户点了「历史」：只把请求写进队列，不自动加载任何东西。
                # agent 通过 blackboard-inbox 取到这条记录后，才去读归档。
                item = {"id": "hist_%s" % uuid.uuid4().hex[:10],
                        "kind": "history", "text": "历史", "at": time.time()}
                os.makedirs(STATE_DIR, exist_ok=True)
                with open(ASK_FILE, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                self._send_json({"ok": True, "id": item["id"]})
                return
            if path == "/api/ask":
                text = str(body.get("text") or "").strip()
                if not text:
                    self._send_json({"ok": False, "error": "empty"}, 400)
                    return
                item = {"id": "ask_%s" % uuid.uuid4().hex[:10], "kind": "question", "text": text,
                        "interrupt": True, "at": time.time()}
                os.makedirs(STATE_DIR, exist_ok=True)
                with open(ASK_FILE, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                # 同时向大脑提一个「立即打断」的申请：
                # 用户从黑板发话时，正在跑的讲解 / 工具调用应该马上停下先看这条。
                # 这是申请，不是保证 —— 插件会把它转成 agent.steer()。
                try:
                    pytools.send_message(
                        "【黑板提问】%s\n（先停下当前任务，读这条并回应；读完后用 "
                        "blackboard-inbox 确认没有遗漏的提问。）" % text,
                        interrupt=True,
                    )
                except Exception as exc:
                    # 申请失败不能拖垮问答本身，内容已经写进 ask.jsonl 了
                    try:
                        with open(LOG_FILE, "a", encoding="utf-8") as handle:
                            handle.write("[%s] 反向申请失败：%s\n" % (
                                time.strftime("%H:%M:%S"), exc))
                    except OSError:
                        pass
                self._send_json({"ok": True, "id": item["id"]})
                return
            if path == "/api/close-policy":
                CLOSE_STATE["allowed"] = bool(body.get("allowed", True))
                self._send_json({"ok": True, "allowed": CLOSE_STATE["allowed"]})
                return
            if path == "/api/action":
                _atomic_write(ACTION_FILE, json.dumps(body, ensure_ascii=False))
                self._send_json({"ok": True})
                return
            self._send_json({"ok": False, "error": "not found"}, 404)

    return Handler


def _serve_forever() -> None:
    global TOKEN
    os.makedirs(STATE_DIR, exist_ok=True)
    TOKEN = secrets.token_urlsafe(16)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler())
    server.daemon_threads = True
    port = int(server.server_address[1])

    scene = _read_json(SCENE_FILE)
    if scene:
        BUS.last = {"ops": scene.get("ops") or [], "js": scene.get("js") or "",
                    "bg": scene.get("bg") or BG_DEFAULT, "seq": 0}

    info = _read_json(SERVER_FILE)
    info.update({"port": port, "token": TOKEN, "pid": os.getpid(), "startedAt": time.time()})
    _atomic_write(SERVER_FILE, json.dumps(info, ensure_ascii=False))
    # 清掉上一次会话遗留的窗口请求文件：它们是「旧指令」，不该在新会话里重放
    for stale in (HISTORY_WINDOW_FILE, HISTORY_CLOSE_FILE):
        try:
            os.remove(stale)
        except OSError:
            pass
    server.serve_forever(poll_interval=0.2)




def _apply_window_icon() -> None:
    """把任务栏 / 标题栏图标换成自己的。

    pywebview 不提供 icon 参数，WinForms 的 Form.Icon 在 WebView2 窗体上也不一定生效。
    所以直接用 Win32：LoadImage 读 ico，再对自己的窗口发 WM_SETICON。
    """
    if os.name != "nt" or not os.path.isfile(ICON_FILE):
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x00000010
        LR_DEFAULTSIZE = 0x00000040
        WM_SETICON = 0x0080
        ICON_SMALL, ICON_BIG = 0, 1

        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]

        small = user32.LoadImageW(None, ICON_FILE, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        big = user32.LoadImageW(None, ICON_FILE, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
        if not small and not big:
            raise OSError("LoadImage 没能加载图标")

        hwnd = _own_window_handle()
        if not hwnd:
            return
        if small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, ctypes.c_void_p(small))
        if big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, ctypes.c_void_p(big))
        # 关键一步：任务栏优先读「窗口类图标」，只发 WM_SETICON 往往不够。
        # 把类图标一并改掉，任务栏才会跟着变。
        try:
            GCLP_HICON = -14   # 大图标
            GCLP_HICONSM = -34 # 小图标
            user32.SetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            user32.SetClassLongPtrW.restype = ctypes.c_void_p
            # 图标句柄是 Python int，直接传会报 "int too long to convert"，
            # 必须包成 c_void_p。
            if big:
                user32.SetClassLongPtrW(hwnd, GCLP_HICON, ctypes.c_void_p(big))
            if small:
                user32.SetClassLongPtrW(hwnd, GCLP_HICONSM, ctypes.c_void_p(small))
        except Exception:
            pass
        # 再强刷一下标题栏，让它立即重绘成新图标
        try:
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0004 | 0x0020)
        except Exception:
            pass

        # 任务栏分组用 AppUserModelID；不设的话，固定到任务栏可能出现 python 图标
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("dsh.blackboard")
        except Exception:
            pass
    except Exception as exc:
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write("[%s] 设置图标失败：%s\n" % (time.strftime("%H:%M:%S"), exc))


def _own_window_handle() -> int:
    """本进程里属于黑板窗口的 HWND。

    无边框窗口的标题可能为空，靠标题筛选会找错甚至找不到，所以这里不按标题筛：
    先收集本进程所有可见顶层窗口，优先取面积最大的那个（那就是黑板）。
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        pid = os.getpid()
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _lparam):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != pid:
                return True
            if not user32.IsWindowVisible(hwnd):
                return True
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
            if area <= 0:
                return True
            found.append((area, hwnd))
            return True

        user32.EnumWindows(visit, 0)
        if not found:
            return 0
        found.sort(reverse=True)
        return int(found[0][1])
    except Exception:
        return 0


def _window_handle_of(pid: int) -> int:
    """按**指定进程号**找它的可见顶层窗口（面积最大的那个）。

    服务进程与窗口进程是两个进程（--serve / --window），
    所以在服务进程里用 os.getpid() 是找不到窗口的，必预传目标 pid。
    """
    if not pid:
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _lparam):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != pid:
                return True
            if not user32.IsWindowVisible(hwnd):
                return True
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
            if area <= 0:
                return True
            found.append((area, hwnd))
            return True

        user32.EnumWindows(visit, 0)
        if not found:
            return 0
        found.sort(reverse=True)
        return int(found[0][1])
    except Exception:
        return 0


def _window_health(pid: int = 0) -> Dict[str, Any]:
    """窗口真的在不在、可不可见。

    为什么要这个：服务进程活着 ≠ 用户看得见黑板。
    以前 /api/ping 只报 pid，于是窗口静默死掉后，open 永远认为「已经开着」，
    再也不会重建 —— 用户面前就是一片空白，而所有接口都说 running: true。
    """
    hwnd = _window_handle_of(pid) if pid else _own_window_handle()
    if not hwnd:
        return {"alive": False, "hwnd": 0, "visible": False,
                "minimized": False, "width": 0, "height": 0}
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        visible = bool(user32.IsWindowVisible(hwnd))
        minimized = bool(user32.IsIconic(hwnd))
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return {
            "alive": True,
            "hwnd": int(hwnd),
            "visible": visible,
            "minimized": minimized,
            "width": max(0, rect.right - rect.left),
            "height": max(0, rect.bottom - rect.top),
        }
    except Exception:
        return {"alive": False, "hwnd": int(hwnd), "visible": False,
                "minimized": False, "width": 0, "height": 0}


def _minimize_window(window: Any) -> None:
    """最小化到任务栏。

    pywebview 的 window.minimize() 在 Windows/WebView2 上不一定生效（实测无效），
    所以直接对窗口句柄发 ShowWindow(SW_MINIMIZE)。
    """
    if os.name == "nt":
        try:
            import ctypes
            hwnd = _own_window_handle()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
                return
        except Exception:
            pass
    try:
        window.minimize()
    except Exception:
        pass


def _restore_window(window: Any) -> None:
    """从任务栏还原。"""
    if os.name == "nt":
        try:
            import ctypes
            hwnd = _own_window_handle()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                return
        except Exception:
            pass
    try:
        window.restore()
    except Exception:
        pass


def _window_main(port: int, token: str, width: int, height: int, on_top: bool) -> int:
    global TOKEN
    TOKEN = token

    # 任务栏图标靠 AppUserModelID 归组，而它必须在「窗口创建之前」就设好，
    # 否则任务栏按 python.exe 的默认图标显示，WM_SETICON 也救不回来。
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("dsh.blackboard")
        except Exception:
            pass

    try:
        import webview
    except Exception as exc:
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%H:%M:%S')}] 导入 pywebview 失败：{exc}\n")
        return 2

    # 关闭策略：统一交给 pywebview 的 closing 事件（下面注册），
    # 不再 monkey-patch on_closing —— 那样会绕过事件系统、也读不到同步过来的策略。
    try:
        from webview.platforms import winforms  # noqa: F401  确认后端可用
    except Exception as exc:
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%H:%M:%S')}] 后端检查：{exc}\n")

    def _install_close_guard(explicit_hwnd: int = 0) -> bool:
        """把关闭拦截挂到真实的窗体事件上。

        pywebview 在窗体初始化时就做了 `self.FormClosing += self.on_closing`，
        这是「实例方法引用」，改类属性根本拦不住。所以得直接往真正窗体的
        FormClosing 上再加一个处理器（后加的会最后执行，能改 args.Cancel）。
        返回 True 表示装上了。
        """
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        target = int(explicit_hwnd) if explicit_hwnd else _own_window_handle()
        if not target:
            return False
        # WM_CLOSE 到不了 Python 层的 FormClosing 代理时，用子类化兜底
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM)

        def _wndproc(hwnd, msg, wparam, lparam):
            if msg == 0x0010:  # WM_CLOSE
                allowed = CLOSE_STATE.get("allowed", True)
                try:
                    with open(os.path.join(STATE_DIR, "close-check.log"), "a", encoding="utf-8") as handle:
                        handle.write("%.3f WM_CLOSE allowed=%s\n" % (time.time(), allowed))
                except OSError:
                    pass
                if not allowed:
                    return 0  # 吃掉这条消息 = 拒绝关闭
            return user32.CallWindowProcW(old_proc_ref[0], hwnd, msg, wparam, lparam)

        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.CallWindowProcW.restype = ctypes.c_longlong
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
        GWLP_WNDPROC = -4

        old_proc_ref = [user32.SetWindowLongPtrW(target, GWLP_WNDPROC,
                                                 ctypes.cast(_wndproc, ctypes.c_void_p))]
        # 保持回调对象存活，否则会被回收后崩溃
        _KEEPALIVE.append(_wndproc)
        with open(os.path.join(STATE_DIR, "close-check.log"), "a", encoding="utf-8") as handle:
            handle.write("installed on hwnd=%s\n" % target)
        return True
    window = webview.create_window(
        PAGE_TITLE,
        f"http://127.0.0.1:{int(port)}/",
        width=width,
        height=height,
        frameless=True,       # 无边框：不要标题栏，不挡画面
        easy_drag=True,       # 窗口任意位置都可拖动（输入框在页面里豁免）
        resizable=True,
        min_size=(420, 300),
        background_color=BG_DEFAULT,
        on_top=False,         # 不悬浮、不置顶
        text_select=True,     # 底部输入框要能选中文字
        confirm_close=False,
    )
    # 关闭策略完全交给上面的 on_closing；这里不再注册 closing 监听器，
    # 否则两个入口会互相干扰（pywebview 会先问 closing，再进 on_closing）。

    # 启动时只校正一次尺寸（尊重用户上次的窗口位置不需要，这里固定落位）
    def place_full() -> None:
        time.sleep(0.6)
        try:
            window.resize(int(width), int(height))
            window.move(120, 60)
        except Exception:
            pass

    threading.Thread(target=place_full, daemon=True).start()

    stop = threading.Event()

    def policy_loop() -> None:
        """每 1.5 秒向服务端问一次「现在允许用户关窗吗」。

        这里不能直接读本进程的 CLOSE_STATE —— 窗口是独立进程，服务端那边
        改的是它自己的变量，只能靠轮询同步。
        """
        while not stop.is_set():
            time.sleep(1.5)
            try:
                import urllib.request
                url = "http://127.0.0.1:%d/api/ping?token=%s" % (int(port), TOKEN)
                with urllib.request.urlopen(url, timeout=2) as response:
                    data = json.loads(response.read().decode("utf-8"))
                CLOSE_STATE["allowed"] = bool(data.get("closeAllowed", True))
            except Exception:
                # 服务端没了就允许关，免得窗口变成关不掉的幽灵
                CLOSE_STATE["allowed"] = True

    threading.Thread(target=policy_loop, daemon=True).start()

    # 「历史」窗口：主窗口 GUI 循环已在跑，新窗口必须在同一个进程里创立。
    # 这里盯一个请求文件，服务端收到 /api/history-window 就写它。
    hist_win_holder: Dict[str, Any] = {}

    def history_window_loop() -> None:
        # 基线：只处理本进程启动之后**新写**的请求文件。
        # 以前这里写的是 seen = 0.0，而 0.0 小于任何文件的 mtime，
        # 于是上次会话遗留的 history-window.request 每次都被当成「用户刚点了历史」，
        # 结果一打开黑板就弹历史窗。
        try:
            seen = os.path.getmtime(HISTORY_WINDOW_FILE)
        except OSError:
            seen = 0.0
        # 基线：只处理本进程启动之后新写的关闭请求，
        # 免得上一次会话遗留的请求把刚开的窗口立刻关掉。
        try:
            seen_close = os.path.getmtime(HISTORY_CLOSE_FILE)
        except OSError:
            seen_close = 0.0
        url = "http://127.0.0.1:%d/history" % int(port)

        def drop_window() -> None:
            """销毁历史窗口并清掉引用。清引用很要紧：
            否则下次点“历史”会去 show() 一个已经死掉的窗口，就再也打不开了。"""
            holder = hist_win_holder.get("window")
            if holder is None:
                return
            try:
                holder.destroy()
            except Exception:
                pass
            hist_win_holder["window"] = None

        # 用 Alt+F4 等其它方式关掉时也要清引用
        def on_closed() -> None:
            hist_win_holder["window"] = None

        while not stop.is_set():
            time.sleep(0.4)

            # ① 处理页面发来的关闭请求
            try:
                close_mtime = os.path.getmtime(HISTORY_CLOSE_FILE)
            except OSError:
                close_mtime = 0.0
            if close_mtime > seen_close:
                seen_close = close_mtime
                drop_window()

            # ② 处理“打开历史”请求
            try:
                mtime = os.path.getmtime(HISTORY_WINDOW_FILE)
            except OSError:
                continue
            if mtime <= seen:
                continue
            seen = mtime
            existing = hist_win_holder.get("window")
            try:
                if existing is not None:
                    # 已经开着：提到前台即可；若已经失效则丢弃重开
                    try:
                        existing.show()
                        existing.restore()
                        continue
                    except Exception:
                        hist_win_holder["window"] = None
                new_window = webview.create_window(
                    "历史",
                    url,
                    width=760,
                    height=620,
                    frameless=True,       # 无边框：不要标题栏
                    easy_drag=False,      # 关掉整窗拖动，靠页面里的 pywebview-drag-region 指定可拖区域
                    resizable=True,
                    min_size=(520, 380),
                    background_color="#0b0e17",
                    on_top=False,
                    text_select=True,
                )
                hist_win_holder["window"] = new_window
                try:
                    new_window.events.closed += on_closed
                except Exception:
                    pass
            except Exception as exc:
                with open(LOG_FILE, "a", encoding="utf-8") as handle:
                    handle.write("[%s] 开历史窗口失败：%s\n" % (time.strftime("%H:%M:%S"), exc))

    threading.Thread(target=history_window_loop, daemon=True).start()

    def controller() -> None:
        # 只执行「本进程启动之后」新写的指令：启动时 action.json 里往往是上一次会话
        # 留下的旧动作（比如 capsule），再跑一次会把窗口立刻缩成小胶囊。
        try:
            baseline = os.path.getmtime(ACTION_FILE)
        except OSError:
            baseline = 0.0
        seen = baseline
        while not stop.is_set():
            time.sleep(0.2)
            try:
                mtime = os.path.getmtime(ACTION_FILE)
            except OSError:
                continue
            if mtime <= seen:
                continue
            seen = mtime
            action = _read_json(ACTION_FILE)
            verb = str(action.get("action") or "")
            try:
                with open(os.path.join(STATE_DIR, "window-actions.log"), "a", encoding="utf-8") as handle:
                    handle.write("%.3f verb=%s action=%r\n" % (time.time(), verb, action))
            except OSError:
                pass
            try:
                if verb == "minimize":
                    _minimize_window(window)
                elif verb == "restore":
                    _restore_window(window)
                elif verb == "move":
                    window.move(int(action.get("x") or 0), int(action.get("y") or 0))
                elif verb == "resize":
                    window.resize(int(action.get("w") or DEFAULT_W), int(action.get("h") or DEFAULT_H))
                elif verb == "close":
                    stop.set()
                    window.destroy()
                    break
                _atomic_write(DONE_FILE, json.dumps({"done": verb, "at": time.time()}, ensure_ascii=False))
            except Exception as exc:
                _atomic_write(DONE_FILE, json.dumps({"done": verb, "error": str(exc)}, ensure_ascii=False))

    threading.Thread(target=controller, daemon=True).start()

    # 图标：窗体创建后立刻换（宿循环未起之前设最稳）
    def set_icon() -> None:
        for delay in (0.3, 1.0, 2.0):
            time.sleep(delay)
            _apply_window_icon()

    threading.Thread(target=set_icon, daemon=True).start()

    def install_guard_later() -> None:
        # 等 pywebview 把 window.native（原生句柄）填上再装
        for _ in range(80):
            time.sleep(0.25)
            try:
                native = getattr(window, "native", None)
                if native is not None:
                    handle = int(getattr(native, "Handle", native))
                    if handle and _install_close_guard(handle):
                        return
            except Exception:
                continue

    threading.Thread(target=install_guard_later, daemon=True).start()

    try:
        webview.start()
    finally:
        stop.set()
    return 0


def _window_launcher() -> Optional[List[str]]:
    """窗口进程用哪个命令启动。

    任务栏图标跟的是 **exe 文件**，只要窗口跑在 python.exe 里，任务栏就永远显示
    Python 的图标（WM_SETICON / 类图标都救不回来）。如果旁边有打包好的
    blackboard.exe（带我们自己的图标），就优先用它。
    """
    exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blackboard.exe")
    if os.name == "nt" and os.path.isfile(exe):
        return [exe, "--window"]
    return None


def _spawn_window(port: int, token: str, width: int, height: int, on_top: bool) -> int:
    log = _open_log()
    launcher = _window_launcher()
    if launcher is not None:
        argv = launcher + [str(port), token, str(width), str(height), "1" if on_top else "0"]
    else:
        argv = [_python(), os.path.abspath(__file__), "--window", str(port), token,
                str(width), str(height), "1" if on_top else "0"]
    child = subprocess.Popen(  # noqa: S603
        argv,
        stdout=log, stderr=log, stdin=subprocess.DEVNULL, cwd=STATE_DIR, **_detached_flags(),
    )
    return int(child.pid)


def _hard_shutdown() -> None:
    """关掉服务进程，但不归档。

    用于「窗口死了、需要重建」：用户什么都没做错，不应该被归档。
    blackboard_close 走的是另一条路（先归档再关）。
    """
    info = _read_json(SERVER_FILE)
    pid = info.get("pid")
    if _pid_alive(pid):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except Exception:
            pass
        deadline = time.time() + 5
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.1)
        if _pid_alive(pid):
            try:
                os.kill(int(pid), 9)
            except Exception:
                pass
    win_pid = info.get("windowPid")
    if _pid_alive(win_pid) and int(win_pid) != int(pid or 0):
        try:
            os.kill(int(win_pid), signal.SIGTERM)
        except Exception:
            pass
    try:
        os.remove(SERVER_FILE)
    except OSError:
        pass


def _window_ok(ping: Dict[str, Any]) -> bool:
    """窗口是不是真的能看、而且在画。"""
    win = ping.get("window") or {}
    if not win.get("alive"):
        return False
    if not win.get("visible"):
        return False
    if win.get("minimized"):
        # 最小化不算坏——用户自己收起来的，open 负责唤醒就行
        return True
    if int(win.get("width") or 0) <= 0 or int(win.get("height") or 0) <= 0:
        return False
    return True


def _canvas_info() -> Dict[str, Any]:
    """画布可用的绘图区域（CSS 像素）——agent 靠它决定内容画多大。

    宽度 = 窗口内容宽；高度 = 窗口高 - 底部问答栏。
    """
    info = _read_json(SERVER_FILE)
    win = info.get("windowSize") or {}
    w = int(win.get("width") or 0)
    h = int(win.get("height") or 0)
    if w <= 0 or h <= 0:
        # 窗口尺寸未知时，按上一次记录的尺寸，再不行用默认值
        w = int(info.get("width") or DEFAULT_W)
        h = int(info.get("height") or DEFAULT_H)
    return {
        "width": w,
        "height": max(1, h - ASK_BAR_H),
        "askBarHeight": ASK_BAR_H,
        "note": "坐标原点在左上角；高度已扣掉底部问答栏",
    }


def _wait_render(seq: Any, timeout: float = 2.0) -> bool:
    """等页面确认「这一帧已经画出来了」。

    seq 传 None 就只要求 renderedSeq > 0（重建后重推的场景）。
    返回 False 就说明窗口可能在静默死掉 —— 帧推得进去，但没人画。
    """
    try:
        want = int(seq) if seq is not None else 1
    except (TypeError, ValueError):
        want = 1
    deadline = time.time() + timeout
    while time.time() < deadline:
        ping = _http("GET", "/api/ping", timeout=2.0)
        if not ping.get("ok"):
            return False
        rendered = int(ping.get("renderedSeq") or 0)
        if rendered >= want:
            return True
        time.sleep(0.1)
    return False


def _ensure_running(width: int = DEFAULT_W, height: int = DEFAULT_H, on_top: bool = True) -> Dict[str, Any]:
    if _server_alive():
        info = _read_json(SERVER_FILE)
        ping = _http("GET", "/api/ping", timeout=3.0)
        if _window_ok(ping):
            return {"ok": True, "started": False, **info}
        # 服务活着，但窗口没了/看不见 —— 以前这里直接返回「已开着」，
        # 于是用户面前永远是空白，而所有接口都报正常。
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as handle:
                handle.write("[%s] 窗口不健康：%s；踢掉旧服务后重建\n" % (
                    time.strftime("%H:%M:%S"),
                    json.dumps(ping.get("window") or {}, ensure_ascii=False)))
        except OSError:
            pass
        _hard_shutdown()
    log_obj = _open_log()
    subprocess.Popen(  # noqa: S603
        [_python(), os.path.abspath(__file__), "--serve", str(width), str(height),
         "1" if on_top else "0"],
        stdout=log_obj, stderr=log_obj, stdin=subprocess.DEVNULL, cwd=STATE_DIR, **_detached_flags(),
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        if _server_alive():
            info = _read_json(SERVER_FILE)
            if not _pid_alive(info.get("windowPid")):
                await_window = time.time() + 20
                while time.time() < await_window and not info.get("windowPid"):
                    time.sleep(0.2)
                    info = _read_json(SERVER_FILE)
            return {"ok": True, "started": True, **(info or {})}
        time.sleep(0.3)
    return {"ok": False, "error": f"黑板服务没能在 30 秒内起来，看日志：{LOG_FILE}"}




@register(description="打开（或唤起）黑板窗口：普通窗口、可拖动、不置顶、带底部问答框。", timeout=90)
def blackboard_open(width: int = DEFAULT_W, height: int = DEFAULT_H, on_top: bool = True) -> Dict[str, Any]:
    """打开黑板窗口。

    Args:
        width: 窗口宽度（CSS 像素），默认 1120。
        height: 窗口高度，默认 720。
        on_top: 是否置顶，默认 True。
    """
    result = _ensure_running(width, height, on_top)
    if result.get("ok"):
        result["url"] = f"http://127.0.0.1:{result.get('port')}/"
        result["rules"] = ("Windows 普通窗口（带标题栏，可拖动，不置顶）；右上角横线 = 最小化到任务栏；"
                          "底部问答框双击才发送；用户关不掉，只有 blackboard_close 能关。")
    return result


@register(description="调黑板显示缩放（不改画布坐标）。每次 draw 也会返回当前画布尺寸。", timeout=30)
def blackboard_zoom(percent: float = 100) -> Dict[str, Any]:
    """设置黑板显示缩放比例。

    Args:
        percent: 百分比，100 = 原始大小。范围 50~250。
    """
    if not _server_alive():
        started = _ensure_running()
        if not started.get("ok"):
            return started
    value = max(50.0, min(250.0, float(percent)))
    result = _http("POST", "/api/zoom", {"percent": value}, timeout=20)
    if result.get("ok"):
        result["canvas"] = _canvas_info()
        result["hint"] = "缩放只改显示比例，画布坐标不变"
    return result


@register(description="把一张静态画面推上黑板，窗口立即显示。ops 见帧协议。", timeout=60)
def blackboard_draw(ops: List[Dict[str, Any]], bg: str = BG_DEFAULT, title: str = "") -> Dict[str, Any]:
    """推一帧静态画面。

    Args:
        ops: 绘图指令列表（rect / circle / line / poly / path / text / image / clear）。
        bg: 背景色，默认 #0b0e17。
        title: 可选标题。
    """
    if not isinstance(ops, list):
        return {"ok": False, "error": "ops 必须是列表"}
    if not _server_alive():
        started = _ensure_running()
        if not started.get("ok"):
            return started
    result = _http("POST", "/api/draw", {"ops": ops, "bg": bg, "title": title})
    if result.get("ok"):
        _atomic_write(SCENE_FILE, json.dumps({"ops": ops, "bg": bg, "title": title}, ensure_ascii=False))
        seq = result.get("seq")
        if _wait_render(seq):
            result["rendered"] = True
        else:
            # 推上去了但页面没确认 —— 窗口可能在静默死掉。
            # 不等连续 N 次失败，这一帧就重建并重推，用户少看一眼空白。
            try:
                with open(LOG_FILE, "a", encoding="utf-8") as handle:
                    handle.write("[%s] 第 %s 帧未确认渲染，重建窗口\n" % (
                        time.strftime("%H:%M:%S"), seq))
            except OSError:
                pass
            rebuilt = _ensure_running()
            if rebuilt.get("ok"):
                _http("POST", "/api/draw", {"ops": ops, "bg": bg, "title": title})
                result["rebuilt"] = True
                result["rendered"] = _wait_render(None)
    return result


@register(description="在黑板里跑一段自包含 JS：定义了 frame(env) 就每帧调用（自写动画）；否则执行一次。", timeout=60)
def blackboard_eval(code: str, title: str = "") -> Dict[str, Any]:
    """在黑板页面里执行 JS。

    Args:
        code: JS 源码。定义 frame(env) 则每帧调用：env.t / env.w / env.h / env.draw([...ops])。
              没有 frame 则当作一次性脚本执行（可直接操作 canvas）。
        title: 可选标题。
    """
    if not _server_alive():
        started = _ensure_running()
        if not started.get("ok"):
            return started
    result = _http("POST", "/api/anim", {"js": code, "title": title})
    if result.get("ok"):
        _atomic_write(SCENE_FILE, json.dumps({"ops": [], "js": code, "bg": BG_DEFAULT, "title": title},
                                             ensure_ascii=False))
    return result


@register(description="看黑板状态：服务/窗口是否在跑、窗口是否真的可见、画面序号、渲染进度、待处理提问、讲解进度。", timeout=30)
def blackboard_status() -> Dict[str, Any]:
    """返回黑板状态。

    注意区分两个事实：
      running       服务进程活着、接口能通
      windowAlive   真的有窗口，而且屏幕上看得见
    前者 true 不代表用户看得见黑板 —— 要看后者。
    """
    info = _read_json(SERVER_FILE)
    alive = _server_alive()
    status: Dict[str, Any] = {"ok": True, "running": alive, "port": info.get("port"),
                              "serverPid": info.get("pid"), "windowPid": info.get("windowPid")}
    if alive:
        ping = _http("GET", "/api/ping", timeout=3)
        win = ping.get("window") or {}
        status["seq"] = ping.get("seq")
        status["renderedSeq"] = ping.get("renderedSeq")
        status["windowAlive"] = bool(win.get("alive"))
        status["windowVisible"] = bool(win.get("visible"))
        status["windowMinimized"] = bool(win.get("minimized"))
        status["windowSize"] = "%dx%d" % (win.get("width") or 0, win.get("height") or 0)
        status["url"] = f"http://127.0.0.1:{info.get('port')}/"
        # 帧推了但页面没跟上 → 窗口在但没在画
        seq = int(ping.get("seq") or 0)
        rendered = int(ping.get("renderedSeq") or 0)
        status["stale"] = bool(seq > 0 and rendered < seq)
        if not _window_ok(ping):
            status["hint"] = "窗口不健康（可能已静默死掉）—— 调用 blackboard-open 会自动重建"
        elif status["windowMinimized"]:
            status["hint"] = "窗口在任务栏里（最小化），调用 blackboard-open 或 blackboard-restore 唤醒"
        elif status["stale"]:
            status["hint"] = "帧已推送但页面未确认渲染（renderedSeq 落后 seq）"
        else:
            status["hint"] = "窗口正常可见"
    else:
        status["windowAlive"] = False
        status["windowVisible"] = False
        status["hint"] = "服务没在跑"
    pending = _pending_asks()
    status["pendingQuestions"] = len(pending)
    if pending:
        status["questions"] = [item.get("text") for item in pending]
    lecture = _read_json(LECTURE_FILE)
    if lecture:
        status["lecture"] = lecture
    return status


def _pending_asks() -> List[Dict[str, Any]]:
    """读出尚未取走的提问（取走才算消费，见 blackboard_inbox）。"""
    items: List[Dict[str, Any]] = []
    try:
        with open(ASK_FILE, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return items


@register(description="点右上角横线：把黑板最小化到任务栏（不是缩成小窗）。", timeout=30)
def blackboard_minimize() -> Dict[str, Any]:
    """最小化到任务栏。"""
    return _http("POST", "/api/minimize")


@register(description="从任务栏还原黑板窗口。", timeout=30)
def blackboard_restore() -> Dict[str, Any]:
    """还原窗口。"""
    return _http("POST", "/api/action", {"action": "restore"})


@register(description="取走用户在黑板底部输入框提出的问题（取走即清空）。", timeout=30)
def blackboard_inbox(peek: bool = False) -> Dict[str, Any]:
    """读用户提问。

    Args:
        peek: 只查看不消费（默认 False：取走即清空）。
    """
    items = _pending_asks()
    if items and not peek:
        try:
            os.remove(ASK_FILE)
        except OSError:
            pass
    return {"ok": True, "count": len(items), "questions": items}


@register(description="记录/读取讲解进度：用户提问打断后，从这里接着往下讲。", timeout=30)
def blackboard_lecture(topic: str = "", screen: Optional[int] = None,
                       total: Optional[int] = None, done: bool = False) -> Dict[str, Any]:
    """维护讲解进度。

    Args:
        topic: 讲解主题（首次写入时必填，之后留空保持原值）。
        screen: 当前讲到第几屏（1 起）。
        total: 一共几屏。
        done: 是否已讲完。
    """
    state = _read_json(LECTURE_FILE)
    if topic:
        state["topic"] = topic
    if isinstance(screen, int):
        state["screen"] = screen
    if isinstance(total, int):
        state["total"] = total
    state["done"] = bool(done)
    state["updatedAt"] = time.time()
    _atomic_write(LECTURE_FILE, json.dumps(state, ensure_ascii=False))
    return {"ok": True, "lecture": state}


@register(description="设置用户是否能自己关栅板窗口（AI 停止思考时允许，思考中禁止）。", timeout=30)
def blackboard_allow_close(allowed: bool = True) -> Dict[str, Any]:
    """控制用户能否自己关窗。

    Args:
        allowed: True = 允许用户关（点击/Alt+F4 都行）；False = 拦截。
    """
    return _http("POST", "/api/close-policy", {"allowed": bool(allowed)})


@register(description="把黑板窗口的尺寸/位置恢复成默认（普通窗口，不置顶）。", timeout=30)
def blackboard_reset_window(w: Optional[int] = None, h: Optional[int] = None) -> Dict[str, Any]:
    """重置窗口。

    Args:
        w: 宽度（可选）。
        h: 高度（可选）。
    """
    payload: Dict[str, Any] = {"action": "resize",
                               "w": int(w or DEFAULT_W), "h": int(h or DEFAULT_H)}
    return _http("POST", "/api/action", payload)


HISTORY_WINDOW_FILE = os.path.join(STATE_DIR, "history-window.request")
# 用户点历史窗口的 ✕ 时写这个文件；窗口进程读它就销毁窗口
HISTORY_CLOSE_FILE = os.path.join(STATE_DIR, "history-window-close.request")


def _spawn_history_window(port: int) -> bool:
    """让主窗口进程开一个「历史」窗口。

    主窗口的 GUI 循环已经跑起来了，在别的进程里再起一个 pywebview 容易撞；
    所以这里只写一个请求文件，由主窗口进程自己的轮询线程去开。
    """
    try:
        _atomic_write(HISTORY_WINDOW_FILE, json.dumps({"at": time.time()}, ensure_ascii=False))
        return True
    except OSError:
        return False


def _archive_lecture() -> Dict[str, Any]:
    """把本次讲解归档到 history/，然后清掉所有“上一场”的痕迹。

    归档后下一次开黑板必须是干净的：画面、讲解进度、残留提问全部清掉。
    """
    snapshot = _http("GET", "/api/archive", timeout=10) if _server_alive() else {}
    lecture = (snapshot or {}).get("lecture") or _read_json(LECTURE_FILE)
    screens = (snapshot or {}).get("screens") or []
    questions = (snapshot or {}).get("questions") or []

    topic = str(lecture.get("topic") or "").strip()
    if not topic and not screens:
        return {"ok": True, "archived": False, "reason": "没有讲解记录，无需归档"}

    # 任务简述：≤10 字。优先 topic，没 topic 就取首屏标题。
    first_title = ""
    if screens:
        first_title = str(screens[0].get("title") or "").strip()
    brief = (topic or first_title or "未命名讲解")[:10]

    record = {
        "topic": topic or "未命名讲解",
        "brief": brief,
        "total": lecture.get("total"),
        "screen": lecture.get("screen"),
        "completed": bool(lecture.get("done")),
        "startedAt": lecture.get("updatedAt"),
        "endedAt": time.time(),
        "screens": screens,
        # 起始页面缩略图：首屏的 ops（历史窗口直接用它重绘 canvas）
        "thumb": (screens[0].get("ops") or []) if screens else [],
        "thumbJs": (screens[0].get("js") or "") if screens else "",
        "thumbBg": (screens[0].get("bg") or BG_DEFAULT) if screens else BG_DEFAULT,
        "questions": questions,
        "summary": [s.get("title") or "(无标题)" for s in screens],
    }

    os.makedirs(HISTORY_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", record["topic"])[:40].strip("-") or "lecture"
    path = os.path.join(HISTORY_DIR, "%s-%s.json" % (stamp, safe))
    try:
        _atomic_write(path, json.dumps(record, ensure_ascii=False, indent=1))
    except OSError as exc:
        # 归档失败就保留原状态，下次关闭再试，不丢数据
        return {"ok": False, "archived": False, "error": str(exc)}

    # 归档成功：清理“上一场”的所有痕迹，保证下次开黑板是干净的
    for state_path in (LECTURE_FILE, ASK_FILE, SCENE_FILE):
        try:
            os.remove(state_path)
        except OSError:
            pass

    return {"ok": True, "archived": True, "path": path,
            "topic": record["topic"], "screens": len(screens),
            "questions": len(questions), "completed": record["completed"]}


def _history_records() -> List[Dict[str, Any]]:
    """读出所有归档的「列表视图」：简述 + 缩略图 + 屏数 + 时间。

    旧归档可能没有 brief/thumb 字段，这里就地兼容推出来，不改原文件。
    """
    items: List[Dict[str, Any]] = []
    if not os.path.isdir(HISTORY_DIR):
        return items
    for filename in sorted(os.listdir(HISTORY_DIR), reverse=True):
        if not filename.endswith(".json"):
            continue
        record = _read_json(os.path.join(HISTORY_DIR, filename))
        if not record:
            continue
        screens = record.get("screens") or []
        topic = str(record.get("topic") or "").strip()
        first_title = str((screens[0].get("title") if screens else "") or "").strip()
        brief = str(record.get("brief") or topic or first_title or "未命名讲解")[:10]
        thumb = record.get("thumb")
        if thumb is None:
            thumb = (screens[0].get("ops") or []) if screens else []
        items.append({
            "name": filename,
            "brief": brief,
            "topic": topic,
            "screens": len(screens),
            "completed": bool(record.get("completed")),
            "endedAt": record.get("endedAt"),
            "thumb": thumb,
            "thumbJs": record.get("thumbJs") or ((screens[0].get("js") or "") if screens else ""),
            "thumbBg": record.get("thumbBg") or ((screens[0].get("bg") or BG_DEFAULT) if screens else BG_DEFAULT),
        })
    return items


@register(description="打开「历史」窗口：缩略图 + 简述列表，左键载入、右键删除、底部清空。", timeout=30)
def blackboard_history_window() -> Dict[str, Any]:
    """打开或唤起历史窗口。"""
    if not _server_alive():
        started = _ensure_running()
        if not started.get("ok"):
            return started
    return _http("POST", "/api/history-window")


@register(description="读黑板讲解历史：列目录，或加载某一场的完整画面供继续讲解。", timeout=60)
def blackboard_history(name: str = "", screen: int = 0) -> Dict[str, Any]:
    """读历史归档。

    Args:
        name: 归档文件名（不传就列目录）。
        screen: 只取第几屏（1 起，0 = 全部）。
    """
    if not os.path.isdir(HISTORY_DIR):
        return {"ok": True, "count": 0, "items": []}
    files = sorted(
        (f for f in os.listdir(HISTORY_DIR) if f.endswith(".json")),
        reverse=True,
    )
    if not name:
        items = []
        for filename in files[:50]:
            record = _read_json(os.path.join(HISTORY_DIR, filename))
            items.append({
                "name": filename,
                "topic": record.get("topic"),
                "screens": len(record.get("screens") or []),
                "completed": record.get("completed"),
                "endedAt": record.get("endedAt"),
            })
        return {"ok": True, "count": len(items), "items": items}

    safe = os.path.basename(name)
    path = os.path.join(HISTORY_DIR, safe)
    if not os.path.isfile(path):
        return {"ok": False, "error": "找不到归档：%s" % safe}
    record = _read_json(path)
    screens = record.get("screens") or []
    if screen > 0:
        if screen > len(screens):
            return {"ok": False, "error": "只有 %d 屏" % len(screens)}
        screens = [screens[screen - 1]]
        record["screen"] = screen
    record["screens"] = screens
    record["name"] = safe
    return {"ok": True, "history": record}


@register(description="关闭黑板（用户在窗口上关不掉；只有这个工具能关）。会先把讲解归档。", timeout=60)
def blackboard_close() -> Dict[str, Any]:
    """归档本次讲解，然后关闭黑板窗口与服务。"""
    archive = _archive_lecture()
    result = _http("POST", "/api/action", {"action": "close"}, timeout=5)
    info = _read_json(SERVER_FILE)
    time.sleep(1.0)
    for pid in (info.get("windowPid"), info.get("pid")):
        if isinstance(pid, int) and _pid_alive(pid):
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=10,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception:
                pass
    for path in (SERVER_FILE, ACTION_FILE, DONE_FILE):
        try:
            os.remove(path)
        except OSError:
            pass
    return {"ok": True, "closed": True, "archive": archive, "detail": result}


@register(description="Python 侧动画：把一组画面按间隔依次推上黑板，逐帧实时显示。", timeout=600)
def blackboard_run(frames: List[List[Dict[str, Any]]], interval: float = 0.08,
                   bg: str = BG_DEFAULT, ctx: Any = None) -> Dict[str, Any]:
    """逐帧播放。

    Args:
        frames: 每一帧是一组 ops。
        interval: 帧间隔秒数（0.02..2）。
        bg: 背景色。
    """
    if not _server_alive():
        started = _ensure_running()
        if not started.get("ok"):
            return started
    gap = max(0.02, min(2.0, float(interval)))
    total = len(frames or [])
    sent = 0
    for index, ops in enumerate(frames or []):
        result = _http("POST", "/api/draw", {"ops": ops, "bg": bg, "title": f"frame {index}"})
        if not result.get("ok"):
            return {"ok": False, "sent": sent, "error": result.get("error")}
        sent += 1
        try:
            ctx.progress(sent / max(1, total), f"第 {sent}/{total} 帧")
        except Exception:
            pass
        time.sleep(gap)
    if frames:
        _atomic_write(SCENE_FILE, json.dumps({"ops": frames[-1], "bg": bg, "title": "frames"}, ensure_ascii=False))
    return {"ok": True, "frames": sent, "interval": gap}




skill(
    "blackboard-guide",
    description="怎么用黑板讲课：帧协议、自包含动画、图案配方、窗口规矩",
    when_to_use="需要在黑板上画图案、写公式、跑动画、做演示时",
    body=r"""# 黑板作画

## 链路

`blackboard_open` 打开窗口 → `blackboard_draw(ops)` 推一帧 → 窗口立刻变。
要持续动起来，用 `blackboard_eval(code)` 推一个 `frame(env)`，页面自己跑
requestAnimationFrame，每帧调用它，不用来回调工具（更快、更省）。

## 先看窗口尺寸，再决定内容画多大

**这是最容易犯的错：自己拍一个尺寸，结果内容超出窗口被切掉，或者小得看不清。**

每次 `blackboard_draw` 返回里都有 `canvas`：

```json
{
  "ok": true, "seq": 12, "rendered": true,
  "canvas": { "width": 1150, "height": 698, "askBarHeight": 62,
              "note": "坐标原点在左上角；高度已扣掉底部问答栏" }
}
```

- `width` / `height` 是**真正能画的区域**（CSS 像素），高度已经扣掉底部问答框
- 所有坐标必须落在这个矩形内；`x + w <= width`，`y + h <= height`
- 想要窗口尺寸自己先看 `blackboard_open` 的 `windowSize`，或者 `blackboard_status`

### 排版建议

| 用途 | 建议尺寸（相对画布） |
|---|---|
| 大标题 | `size` 36~48，占宽度 60% 左右 |
| 正文 | `size` 18~24 |
| 注释 | `size` 14~16 |
| 边距 | 四周留 40~60px，不要贴边 |
| 分栏 | 三栏时每栏宽 ≈ (width - 边距*4) / 3 |

**文字放得下的判断**：中文按 `字数 × size` 估算宽度，超过可用宽度就会溢出。
宁可换行或缩小字号，不要让它跑到窗口外。

## 缩放：用户能调，你也能调

**用户侧**：Ctrl + 滚轮，或者 Ctrl + 加号/减号/Ctrl+0。右上角会显示当前比例。

**你侧**：`blackboard_zoom(percent=80)` —— 传 50~250。

缩放**只改显示比例，不改画布坐标**。也就是说：

- 你仍是按 `canvas.width/height` 这个坐标系画
- 用户放大到 150%，看到的是同一个画面整体放大，不会错位
- 不要在代码里自己根据缩放去算坐标

什么时候该调缩放：

| 情况 | 做法 |
|---|---|
| 内容很多、字很小 | 不要调缩放，**减少内容或分屏** |
| 内容太少、屏幕空荡荡 | 靠增大字号 / 加内容，不是靠缩放 |
| 用户主动说“看不清” | 调 `blackboard_zoom` 或让用户自己 Ctrl+滚轮 |

**默认不要动缩放**（保持 100%），只在用户明确要求时调。

## 讲课：一页对应一句旁白

**黑板和字幕是两条通道，不会自己对齐。** 黑板 `draw` 约 100ms 就返回，
但字幕要念好几秒 —— 不等就会“黑板飞速翻页，字幕还在第一句”。

唯一可靠的节奏是：**画一页 → 讲完 → 再画下一页**。

```python
for page in pages:
    blackboard_draw(page.ops)                  # ① 先出画面
    talk_say(page.narration, wait=True)        # ② 讲，等它真的念完
    # 到这里才翻下一页
```

`talk_say(wait=True)` 会阻塞到这句真正念完（或被中断），返回：

```json
{"finished": true, "interrupted": false, "elapsedSeconds": 9.5}
```

### 错误做法对照

| 错误 | 后果 |
|---|---|
| 连续 `blackboard_draw` 不等待 | 黑板飞速翻页，字幕还在念第一句 |
| 只 `talk_say` 不 `draw` | 只有声音没有画面 |
| 用 `talk_status` 轮询代替 `wait=True` | 浪费回合，还可能错过瞬间 |
| 自己 `sleep(秒数)` | 估不准：中文语速、标点停顿、合成耗时都会影响 |

### 用户插话：立即停下，不许收尾

**这是最容易犯错的地方。用户说了新东西，你的第一反应必须是停，不是“把当前这屏讲完”。**

#### 绝对不要做的事

| 错误反应 | 为什么错 |
|---|---|
| “先把这屏讲完再回应” | 用户已经不要这个了，你多讲的每一句都是浪费 |
| “先把剩下的屏归档” | 没人要求归档，用户只想要新话题 |
| “记一下进度，稍后继续” | 除非用户明确说“等下再讲” |

**进度（`blackboard_lecture` 里那个 N/M）不是必须走完的承诺。**
用户换话题，等于旧进度作废。

#### 正确顺序

```python
# 1. 立即停：字幕闭嘴、清空
talk_interrupt("用户换话题了")

# 2. 立刻切到新话题第 1 屏
blackboard_lecture(topic="新话题", screen=1, total=N)
blackboard_draw(new_page_ops)
talk_say(new_narration, wait=True)
```

三步连着做，中间不要插任何“上回说到”之类的话。

#### 三种情况的区别

| 用户说的 | 你要做的 |
|---|---|
| 新话题（“讲一下 X”） | `talk_interrupt` → 直接开新话题 |
| 追问细节（“这个再说说”） | `talk_interrupt` → 就着当前页补充 → 继续原进度 |
| 让你停（“停”“算了”） | `talk_interrupt` → 什么都不再说 |

只有第二种才需要“继续原进度”。第一种直接丢掉旧话题。

#### 为什么用 `talk_interrupt` 而不是 `talk_stop`

两者都能停，但：

- `talk_interrupt` 会让正在 `talk_say(wait=True)` 的调用**立即返回**
- `talk_stop` 不会 —— 等待方会卡到超时

所以你在等旁白念完时，用户插话，用 `talk_stop` 会让你卡住好几秒才能回应。

### 记录讲到哪了

每页讲之前调 `blackboard_lecture(topic=..., screen=N, total=M)`。

它的用途是：**被打断后知道该从哪接**。不是“必须把 N 讲完”。
用户换话题时直接开新话题，旧进度自然作废。

## 帧协议

坐标是画布区域的 CSS 像素，左上角 (0,0)。画之前先确认范围（见上一节）。

```python
[
  {"op": "clear",  "bg": "#0b0e17"},
  {"op": "rect",   "x": 40, "y": 40, "w": 320, "h": 150, "color": "#1f3b57", "r": 14},
  {"op": "circle", "x": 240, "y": 300, "r": 60, "color": "#ffd400", "fill": False, "width": 3},
  {"op": "line",   "x1": 40, "y1": 500, "x2": 600, "y2": 500, "color": "#8be9fd", "width": 2},
  {"op": "poly",   "points": [[520,80],[620,80],[570,160]], "color": "#50fa7b", "close": True, "fill": True},
  {"op": "path",   "d": "M40 640 C 160 540, 300 740, 460 640", "color": "#ff79c6", "width": 4},
  {"op": "text",   "x": 48, "y": 104, "text": "标题", "size": 34, "color": "#f8f8f2", "weight": "600"},
  {"op": "text",   "x": 560, "y": 300, "text": "居中", "size": 20, "align": "center", "color": "#aaa"},
]
```

- `fill: false` 只描边；`alpha` 调透明度；`align` 取 left / center / right。
- 讲重点时用亮色（`#ffd400` 黄、`#ff5f56` 红）圈一下，别用太多颜色。

## 自包含动画

```python
blackboard_eval(r'''
function frame(env) {
  const { t, w, h } = env;
  const ops = [];
  for (let i = 0; i < 48; i++) {
    const a = t * 0.8 + i * Math.PI / 24;
    ops.push({ op: "circle", x: w / 2 + Math.cos(a) * 170, y: h / 2 + Math.sin(a) * 170,
               r: 8, color: `hsl(${i * 7}, 90%, 66%)` });
  }
  ops.push({ op: "text", x: w / 2, y: h - 40, text: "t = " + t.toFixed(1),
             size: 18, color: "#8be9fd", align: "center" });
  env.draw(ops);
}
''')
```

每帧开头自己 `{op:"clear"}` 一次，或者不 clear 就是累积笔迹（画轨迹很好用）。

## 讲课的一般节奏

1. `blackboard_open`，先画一张标题页（大面积留白 + 一行大字）；
2. 讲哪个点就推哪一张图，把当前重点用亮色标注；
3. 讲过程（推导、排序、几何变换）用 `frame` 动画；
4. 讲完留着最后一帧，不用急着关。

## 窗口规矩（不要违反）

- 无边框；右上角圆点由用户点，缩小 / 还原；
- **用户关不掉**：Alt+F4、任务栏关闭都会被拦截；
- 只有 `blackboard_close` 能关它，且只在用户明确要求关闭时用。
""",
)




def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["--serve"]:
        width = int(argv[1]) if len(argv) > 1 else DEFAULT_W
        height = int(argv[2]) if len(argv) > 2 else DEFAULT_H
        on_top = (argv[3] if len(argv) > 3 else "1") != "0"
        threading.Thread(target=_serve_forever, daemon=True).start()
        deadline = time.time() + 20
        while time.time() < deadline:
            info = _read_json(SERVER_FILE)
            if info.get("pid") == os.getpid():
                break
            time.sleep(0.1)
        info = _read_json(SERVER_FILE)
        info["windowPid"] = _spawn_window(info["port"], info["token"], width, height, on_top)
        _atomic_write(SERVER_FILE, json.dumps(info, ensure_ascii=False))
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0
    if argv[:1] == ["--window"]:
        return _window_main(
            int(argv[1]), argv[2],
            int(argv[3]) if len(argv) > 3 else DEFAULT_W,
            int(argv[4]) if len(argv) > 4 else DEFAULT_H,
            (argv[5] if len(argv) > 5 else "1") != "0",
        )
    return pytools_main()


if __name__ == "__main__":
    raise SystemExit(main())
