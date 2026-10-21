# blackboard —— 黑板

一块常驻桌面、由 agent 用代码作画并实时呈现的窗口。用来讲课、做演示、画示意图。

```
blackboard.py   全部实现（单文件）
blackboard.ico  窗口图标
history/        讲解归档
```

## 窗口形态

| 项 | 表现 |
|---|---|
| 边框 | **无边框**，没有标题栏 |
| 拖动 | 除底部输入框外，窗口任何地方都能拖 |
| 置顶 | 不置顶，普通窗口 |
| 右上角 | 一条横线 → 点击最小化到任务栏；左边「···」→ 菜单 |
| 底部 | 问答输入框 + 发送按钮（**点发送才发，回车不发**） |
| 关闭 | 用户关不掉（除 AI 停止思考时放行）；只有 `blackboard-close` 能关 |

## 工具

| 工具 | 作用 |
|---|---|
| `blackboard-open` | 打开或唤起窗口 |
| `blackboard-draw` | 推一张静态画面（ops） |
| `blackboard-eval` | 推一段 JS；定义了 `frame(env)` 则每帧调用（自写动画） |
| `blackboard-run` | Python 侧逐帧推一组画面 |
| `blackboard-status` | 看服务/窗口/待处理提问/讲解进度 |
| `blackboard-minimize` / `blackboard-restore` | 最小化到任务栏 / 还原 |
| `blackboard-reset-window` | 窗口尺寸位置恢复默认 |
| `blackboard-allow-close` | 设置用户能否自己关窗 |
| `blackboard-inbox` | 取走用户在底部输入的问题（取走即清空） |
| `blackboard-lecture` | 记录/读取讲解进度（打断后接着讲） |
| `blackboard-history-window` | 打开「历史」窗口 |
| `blackboard-history` | 读归档：列目录，或加载某一场的完整画面 |
| `blackboard-close` | 归档本次讲解，然后关闭 |

## 帧协议

坐标是窗口内容区的 CSS 像素，左上角 (0,0)。

```python
[
  {"op": "clear",  "bg": "#0b0e17"},
  {"op": "rect",   "x": 40, "y": 40, "w": 320, "h": 150, "color": "#1f3b57", "r": 14},
  {"op": "circle", "x": 240, "y": 300, "r": 60, "color": "#ffd400", "fill": False, "width": 3},
  {"op": "line",   "x1": 40, "y1": 500, "x2": 600, "y2": 500, "color": "#8be9fd", "width": 2},
  {"op": "poly",   "points": [[520,80],[620,80],[570,160]], "color": "#50fa7b", "close": True, "fill": True},
  {"op": "path",   "d": "M40 640 C 160 540, 300 740, 460 640", "color": "#ff79c6", "width": 4},
  {"op": "text",   "x": 48, "y": 104, "text": "标题", "size": 34, "color": "#f8f8f2", "weight": "600"},
  {"op": "image",  "x": 0, "y": 0, "w": 100, "h": 100, "src": "data:image/png;base64,..."},
]
```

`fill: false` 只描边；`alpha` 调透明度；`align` 取 left / center / right。

## 动画

```python
blackboard_eval(r'''
function frame(env) {
  const { t, w, h } = env;      // t = 秒
  const ops = [{ op: "clear", bg: "#0b0e17" }];
  ops.push({ op: "circle", x: w/2 + Math.cos(t)*160, y: h/2 + Math.sin(t)*160,
             r: 8, color: "#ffd400" });
  env.draw(ops);
}
''')
```

页面自己跑 `requestAnimationFrame`，每帧调用你的 `frame`，不用来回调工具。

## 讲解与历史

1. 讲课时用 `blackboard-lecture` 记录进度（主题、第几屏、共几屏）
2. 用户中途提问 → `blackboard-inbox` 取到 → 先回答问题，再回到原进度继续
3. 讲完 `blackboard-close` → **自动归档**到 `history/`，并清空状态
4. 下次打开是干净的黑板，从头讲新内容
5. 要接着讲旧的 → 打开「历史」窗口，双击某条载入

归档内容：主题、每屏完整画面、用户提过的问题、是否讲完。

## 安装

```bash
pip install -r requirements.txt
```

## 登记

```bash
pytools install C:\Users\computer\Desktop\blackboard\blackboard.py
```

或在 pytools 面板里输入完整路径点「注册」。

## 已知约束

- **pywebview 在 Windows 上没有实现 `easy_drag` 的完整语义**：实际用的是页面里的 `pywebview-drag-region` 类名 + 捕获阶段拦截输入区事件。
- **`window.close()` 关不掉窗口**：那是浏览器 API，对 pywebview 原生窗口无效；历史窗口的 ✕ 走的是 `/api/history-window-close` 接口。
- **任务栏图标**跟随 exe（python.exe），`WM_SETICON` 只改得到窗口图标，任务栏会回退到 Python 图标。
