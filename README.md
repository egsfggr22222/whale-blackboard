# blackboard
[中文 README](https://github.com/egsfggr22222/whale-blackboard/blob/main/blackboard/README.md)

A window that stays on your desktop, where the agent draws with code and the result is rendered in real time. Used for teaching, giving demos, and sketching diagrams.

```
blackboard.py   Full implementation (single file)
blackboard.ico  Window icon
history/        Lecture archives
```

## Window behavior

| Item | Behavior |
|---|---|
| Border | **Frameless**, no title bar |
| Dragging | Anywhere on the window can be dragged, except the bottom input box |
| Always on top | No — it's a normal window |
| Top right | A horizontal line → click to minimize to the taskbar; to its left, "···" → menu |
| Bottom | Q&A input box + send button (**only clicking send sends it; Enter does not**) |
| Closing | The user can't close it (except when the AI has stopped thinking); only `blackboard-close` can close it |

## Tools

| Tool | Purpose |
|---|---|
| `blackboard-open` | Open or bring up the window |
| `blackboard-draw` | Push a static frame (ops) |
| `blackboard-eval` | Push a piece of JS; if `frame(env)` is defined, it's called every frame (self-written animation) |
| `blackboard-run` | Push a set of frames from the Python side, one at a time |
| `blackboard-status` | Check the service/window/pending questions/lecture progress |
| `blackboard-minimize` / `blackboard-restore` | Minimize to taskbar / restore |
| `blackboard-reset-window` | Reset window size and position to defaults |
| `blackboard-allow-close` | Set whether the user can close the window themselves |
| `blackboard-inbox` | Take the questions the user typed at the bottom (taking them clears them) |
| `blackboard-lecture` | Record/read lecture progress (resume after an interruption) |
| `blackboard-history-window` | Open the "History" window |
| `blackboard-history` | Read archives: list the directory, or load the full frames of a session |
| `blackboard-close` | Archive this lecture, then close |

## Frame protocol

Coordinates are CSS pixels in the window's content area, with the origin at the top-left (0,0).

```python
[
  {"op": "clear",  "bg": "#0b0e17"},
  {"op": "rect",   "x": 40, "y": 40, "w": 320, "h": 150, "color": "#1f3b57", "r": 14},
  {"op": "circle", "x": 240, "y": 300, "r": 60, "color": "#ffd400", "fill": False, "width": 3},
  {"op": "line",   "x1": 40, "y1": 500, "x2": 600, "y2": 500, "color": "#8be9fd", "width": 2},
  {"op": "poly",   "points": [[520,80],[620,80],[570,160]], "color": "#50fa7b", "close": True, "fill": True},
  {"op": "path",   "d": "M40 640 C 160 540, 300 740, 460 640", "color": "#ff79c6", "width": 4},
  {"op": "text",   "x": 48, "y": 104, "text": "Title", "size": 34, "color": "#f8f8f2", "weight": "600"},
  {"op": "image",  "x": 0, "y": 0, "w": 100, "h": 100, "src": "data:image/png;base64,..."},
]
```

`fill: false` draws only the outline; `alpha` sets opacity; `align` takes left / center / right.

## Animation

```python
blackboard_eval(r'''
function frame(env) {
  const { t, w, h } = env;      // t = seconds
  const ops = [{ op: "clear", bg: "#0b0e17" }];
  ops.push({ op: "circle", x: w/2 + Math.cos(t)*160, y: h/2 + Math.sin(t)*160,
             r: 8, color: "#ffd400" });
  env.draw(ops);
}
''')
```

The page runs `requestAnimationFrame` itself and calls your `frame` every frame — no need to call tools back and forth.

## Lectures and history

1. While teaching, use `blackboard-lecture` to record progress (topic, current frame, total frames)
2. If the user asks a question mid-lecture → get it with `blackboard-inbox` → answer it first, then resume from where you left off
3. When done, `blackboard-close` → **automatically archives** to `history/` and clears the state
4. Next time you open it, it's a clean board, ready for a new lecture
5. To continue an old one → open the "History" window and double-click an entry to load it

What gets archived: the topic, every frame's full drawing, the questions the user asked, and whether it was finished.

## Installation

```bash
pip install -r requirements.txt
```

## Registration

```bash
pytools install C:\Users\computer\Desktop\blackboard\blackboard.py
```

Or enter the full path in the pytools panel and click "Register".

## Known constraints

- **pywebview on Windows doesn't implement the full semantics of `easy_drag`**: what's actually used is the `pywebview-drag-region` class name in the page, plus capturing-phase interception of input-area events.
- **`window.close()` can't close the window**: that's a browser API and has no effect on a pywebview native window; the History window's ✕ goes through the `/api/history-window-close` endpoint.
- **The taskbar icon** follows the exe (python.exe); `WM_SETICON` only changes the window icon, and the taskbar falls back to the Python icon.
```
