#!/usr/bin/env python3
"""
言栖 (Yán Qī) — Voice Input for AI Agents (v0.5.0)
=================================================
本地离线识别 (sherpa-onnx + SenseVoice 多语种模型)
  · 支持中文 / 英文 / 日文 / 韩文 / 粤语 自动检测
  · 完全离线, 无需联网, 无配额限制
  · 单文件 ~82MB + 模型 ~230MB
快捷键: 按住 Right Ctrl 录音 → 松开识别 → 自动粘贴
录音期间独占麦克风 (WASAPI Exclusive Mode + 切默认设备)

作者: 孙欣阳 (Xinyang Sun)
项目: https://github.com/Xinyang-S/STT-YanQi

用法: VoiceInput.exe            正常启动
      VoiceInput.exe --minimized   静默启动进托盘 (开机启动用)
      VoiceInput.exe --test        全链路测试

版本: 0.5.0 (pre-release < 1.0.0, 仍在打磨)
"""

import ctypes
import json
import math
import os
import re
import struct
import sys
import tempfile
import threading
import time
import traceback
import wave
from pathlib import Path

# ── 依赖检查 ────────────────────────────────────────────
missing = []
try:    import pyaudio
except ImportError: missing.append("pyaudio")
try:    import numpy as np
except ImportError: missing.append("numpy")
try:    from pynput import keyboard
except ImportError: missing.append("pynput")
try:    import pyperclip
except ImportError: missing.append("pyperclip")
try:    import pyautogui
except ImportError: missing.append("pyautogui")
try:    from PIL import Image, ImageDraw
except ImportError: missing.append("pillow")
try:    import pystray
except ImportError: missing.append("pystray")
try:    import tkinter as tk
except ImportError: missing.append("tkinter")
try:    import queue as queue_mod
except ImportError: missing.append("queue")
try:    import comtypes
except ImportError: missing.append("comtypes")

# 本地 ASR 引擎 (sherpa-onnx + SenseVoice 多语种) — 唯一识别引擎
try:
    import sherpa_onnx
    from sherpa_onnx import OfflineRecognizer
    _HAS_SHERPA = True
except ImportError:
    sherpa_onnx = None
    _HAS_SHERPA = False

if missing:
    import subprocess
    subprocess.run(["msg", "*", f"缺少依赖: {', '.join(missing)}\n\npip install {' '.join(missing)}"], shell=True)
    sys.exit(1)

from tkinter import ttk, messagebox

# ═══════════════════════════════════════════════════════════
#  Discord 风格提示音 (numpy 生成 WAV)
# ═══════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════
#  核心运行时
# ═══════════════════════════════════════════════════════════
# Tauri sidecar 和 legacy Tk shell 共用这一份录音/识别/配置实现，避免核心逻辑重复维护。
from voice_core.runtime import *  # noqa: F401,F403
from voice_core.runtime import (  # noqa: F401
    _HAS_SHERPA,
    _find_wasapi,
    _mic_guard_state,
    _policy_set_default,
    _resolve_model_dir,
)


# ═══════════════════════════════════════════════════════════
#  键盘监听: Right Ctrl 按住录音, Ctrl+Shift+F9 开关
# ═══════════════════════════════════════════════════════════
ctrl_held = False; shift_held = False; rctrl_held = False

def on_press(key):
    global ctrl_held, shift_held, rctrl_held
    if key == keyboard.Key.ctrl_l: ctrl_held = True
    elif key == keyboard.Key.ctrl_r: ctrl_held = True; rctrl_held = True
    elif key == keyboard.Key.shift_l or key == keyboard.Key.shift_r: shift_held = True
    elif key == keyboard.Key.f9 and ctrl_held and shift_held:
        state["enabled"] = not state["enabled"]
        (sound_toggle_on if state["enabled"] else sound_toggle_off)()
        ui_queue.put(("toggled", state["enabled"]))
        return
    if not state["enabled"]: return
    if rctrl_held and not state["recording"]: start_recording()

def on_release(key):
    global ctrl_held, shift_held, rctrl_held
    if key == keyboard.Key.ctrl_l: ctrl_held = False
    elif key == keyboard.Key.ctrl_r:
        rctrl_held = False; ctrl_held = False
        if state["recording"]: stop_recording()
    elif key == keyboard.Key.shift_l or key == keyboard.Key.shift_r: shift_held = False


# ═══════════════════════════════════════════════════════════
#  品牌资源: assets/app_icon.png 优先, 缺失时降级到 PIL 自绘
#  v0.6.1 起所有 icon (托盘/EXE/录音按钮/气泡/关于) 统一用同一张图
# ═══════════════════════════════════════════════════════════
ASSETS_DIR = Path(__file__).parent / "assets"
BRAND_ICON_PATH = ASSETS_DIR / "app_icon.png"  # 用户提供的品牌图 (鸟+声波)
APP_ICO_PATH = Path(__file__).parent / "app.ico"  # 打包到 EXE 的多尺寸 .ico
_brand_img = None
_brand_img_loaded = False

def _resolve_asset_path(filename):
    """在 frozen (PyInstaller) 和开发模式下找到资源文件路径."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p = os.path.join(meipass, filename)
        if os.path.exists(p): return p
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        p = os.path.join(exe_dir, filename)
        if os.path.exists(p): return p
    p = os.path.join(os.getcwd(), filename)
    if os.path.exists(p): return p
    return None

def _load_brand_image():
    """加载品牌图 (PNG, RGBA). 失败返回 None. 结果缓存到 _brand_img."""
    global _brand_img, _brand_img_loaded
    if _brand_img_loaded:
        return _brand_img
    _brand_img_loaded = True
    if not BRAND_ICON_PATH.exists():
        log(f"品牌图缺失: {BRAND_ICON_PATH} (将用降级几何 icon)")
        return None
    try:
        img = Image.open(BRAND_ICON_PATH).convert("RGBA")
        _brand_img = img
        log(f"品牌图加载: {BRAND_ICON_PATH} ({img.size[0]}x{img.size[1]})")
        return img
    except Exception as e:
        log(f"品牌图加载失败: {e!r}")
        return None

def _make_fallback_icon(bg_color, accent_color=(255,255,255,80)):
    """无品牌图时的降级: 平涂圆 + 简化麦克风声波."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([4, 4, 60, 60], fill=bg_color)
    # 麦克风轮廓 (圆角矩形 + 底座)
    d.rounded_rectangle([26, 16, 38, 38], radius=6, fill=accent_color)
    d.arc([22, 22, 42, 42], start=20, end=160, fill=accent_color, width=2)
    d.line([32, 42, 32, 50], fill=accent_color, width=2)
    d.line([26, 50, 38, 50], fill=accent_color, width=2)
    return img

def _tray_icon_for(state_name):
    """根据状态生成托盘 PIL Image. 优先用品牌图 + 不同背景圆, 降级用平涂."""
    base = _load_brand_image()
    # 状态对应背景色
    if state_name == "recording":
        bg = (220, 38, 38, 255)     # 录音红
    elif state_name == "off":
        bg = (156, 163, 175, 255)   # 禁用灰
    else:
        bg = (22, 163, 74, 255)      # 待命绿 #16a34a (与 c["ok"] 同色)
    if base is None:
        return _make_fallback_icon(bg)
    # 鸟图贴在背景圆上: 64x64 透明底, 背景圆 60, 鸟图 56 (放大)
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([0, 0, 64, 64], fill=bg)  # 圆撑满全图 (放大背景)
    bird = base.copy()
    bird.thumbnail((56, 56), Image.LANCZOS)
    # 居中粘贴
    ox = (64 - bird.size[0]) // 2
    oy = (64 - bird.size[1]) // 2
    if state_name == "off":
        # 降饱和: 转 RGBA, alpha 70%
        from PIL import ImageEnhance
        bird = ImageEnhance.Color(bird).enhance(0.0)  # 灰度
        # 提亮底色避免太暗
        alpha = bird.split()[3].point(lambda v: int(v * 0.7))
        bird.putalpha(alpha)
    img.alpha_composite(bird, (ox, oy))
    return img

# 三个状态 tray 图标 (缓存)
ic_idle = _tray_icon_for("idle")
ic_rec  = _tray_icon_for("recording")
ic_off  = _tray_icon_for("off")
G, R, A, Y = (39, 174, 96), (231, 76, 60), (149, 165, 166), (243, 156, 18)
ic_fb = _make_fallback_icon(A)  # 错误状态仍用降级

def get_tray_icon():
    if state["recording"]: return ic_rec
    if not state["enabled"]: return ic_off
    return ic_idle


def _win_colorref(hex_color):
    """Convert #RRGGBB to Windows COLORREF (0x00BBGGRR)."""
    try:
        s = str(hex_color).lstrip("#")
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        return b << 16 | g << 8 | r
    except Exception:
        return 0x00FFFFFF


def _dwm_set_attr(hwnd, attr, value):
    try:
        val = ctypes.c_int(value)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), ctypes.c_int(attr),
            ctypes.byref(val), ctypes.sizeof(val)
        )
        return True
    except Exception:
        return False


def _apply_window_glass(win, alpha=0.985, border="#d2e4f4", caption="#f8fcff"):
    """Best-effort translucent window treatment for Tk on Windows.

    Tkinter cannot blur per-widget backgrounds, so the app uses subtle window
    alpha plus a light glass palette. On Windows 11, ask DWM for rounded corners,
    a light border/titlebar, and an acrylic-like backdrop when available.
    """
    try:
        win.attributes("-alpha", alpha)
    except Exception:
        pass
    if sys.platform != "win32":
        return
    try:
        win.update_idletasks()
        hwnd = win.winfo_id()
        _dwm_set_attr(hwnd, 33, 2)  # DWMWCP_ROUND
        _dwm_set_attr(hwnd, 34, _win_colorref(border))   # DWMWA_BORDER_COLOR
        _dwm_set_attr(hwnd, 35, _win_colorref(caption))  # DWMWA_CAPTION_COLOR
        _dwm_set_attr(hwnd, 36, _win_colorref("#11243a"))  # DWMWA_TEXT_COLOR
        _dwm_set_attr(hwnd, 38, 3)  # DWMSBT_TRANSIENTWINDOW: acrylic-style backdrop
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
#  悬浮气泡 (v0.7.1 玻璃感 + 录音脉动)
#  Why: 用户希望气泡有质感, 录音时有动效反馈.
#  玻璃感: Toplevel 用 -transparentcolor 把指定颜色挖空, Canvas 画半透白圆 +
#           模糊感阴影 = 模拟 macOS 风格毛玻璃.
#  录音动效: 多层脉冲环从中心向外扩散, 颜色从红渐变为透明.
# ═══════════════════════════════════════════════════════════
class FloatingBubble:
    SIZE = 72  # 稍大一点, 给脉冲环留空间
    CLICK_THRESHOLD = 5
    # 玻璃透明色 (canvas bg 用这个色, 然后 Toplevel -transparentcolor 相同)
    _GLASS_KEY = "#010203"

    def __init__(self, main_win):
        self.mw = main_win
        self.c = main_win.c
        self.root = main_win.root
        self.win = None
        self.visible = False
        self._drag_data = {"x": 0, "y": 0, "moved": False}
        self._press_anim = None
        self._press_anim_t = 0
        self._state_cache = None
        self._menu = None
        # 录音脉动: 3 圈, 不同相位
        self._pulses = []   # [(oval_id, phase_offset), ...]
        self._pulse_count = 3
        self._create()

    def _create(self):
        c = self.c
        s = self.SIZE
        # v0.6.6 高级玻璃感: 8 层 Canvas 堆叠 + PIL 渐变图
        self.win = tk.Toplevel(self.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry(f"{s}x{s}")
        self._TRANSPARENT_KEY = "#fafafc"
        self.win.configure(bg=self._TRANSPARENT_KEY)
        try:
            self.win.attributes("-transparentcolor", self._TRANSPARENT_KEY)
        except Exception:
            pass
        self.cv = tk.Canvas(self.win, width=s, height=s,
                            bg=self._TRANSPARENT_KEY, highlightthickness=0, cursor="hand2")
        self.cv.pack()
        # L0 远距扩散阴影 (大, 极淡)
        self._shadow_outer = self.cv.create_oval(-6, 0, s + 6, s + 10,
                                                   fill=c.get("shadow", "#d8e5f0"), outline="")
        # L1 中距阴影 (略深)
        self._shadow_inner = self.cv.create_oval(0, 2, s, s + 6,
                                                   fill=c.get("shadow2", "#c7d7e6"), outline="")
        # L2 玻璃主体 (状态色 fill)
        self._body = self.cv.create_oval(2, 2, s - 2, s - 2, fill="", outline="")
        # L3 状态色 tint (PIL 渐变)
        self._tint_id = self.cv.create_image(s//2, s//2, image="")
        self._glass_tints = {}
        for theme, color in [("glass", "#d7ebff"), ("solid", "#ffb8b3"), ("empty", "#dfe7ef")]:
            self._glass_tints[theme] = self._make_tint(s, color)
        # L4 顶部 specular 高光 (PIL 渐变)
        self._specular_id = self.cv.create_image(s//2, s//2 - 6, image="")
        self._specular_photo = self._make_specular(s)
        # L5 内凹暗影 (玻璃厚度)
        self._inner_shadow = self.cv.create_oval(3, 3, s - 3, s - 3,
                                                   fill="", outline=c.get("glass_border", "#d6e3ef"), width=1)
        # L6 顶部 + 底部 rim (玻璃边缘折射)
        self._rim_top = self.cv.create_arc(3, 3, s - 3, s - 3, start=200, extent=140,
                                           style=tk.ARC, outline="#ffffff", width=1)
        self._rim_bottom = self.cv.create_arc(3, 3, s - 3, s - 3, start=20, extent=140,
                                              style=tk.ARC, outline=c.get("border2", "#c8d6e4"), width=1)
        # L7 品牌图 (中心 60x60)
        self._photo_id = None
        self._photo_ref = None
        if _brand_img is not None:
            from PIL import ImageTk
            bird = _brand_img.copy()
            bird.thumbnail((60, 60), Image.LANCZOS)
            self._photo_ref = ImageTk.PhotoImage(bird)
            self._photo_id = self.cv.create_image(s // 2, s // 2, image=self._photo_ref)
        # 录音态: 中心 ■ 方块
        self._rec_dot = self.cv.create_rectangle(s//2-11, s//2-11, s//2+11, s//2+11,
                                                 fill="#ffffff", outline="")
        self.cv.itemconfigure(self._rec_dot, state="hidden")
        # 录音脉冲环: 3 圈柔和粉红系
        pulse_colors = ["#f87171", "#fca5a5", "#fecaca"]
        for i in range(self._pulse_count):
            oval = self.cv.create_oval(0, 0, 0, 0, outline=pulse_colors[i], width=2)
            self.cv.itemconfigure(oval, state="hidden")
            self._pulses.append((oval, i * 0.33))
        # 拖动用 <Motion>
        self._pressed = False
        self.win.bind("<ButtonPress-1>", self._on_press)
        self.win.bind("<ButtonRelease-1>", self._on_release)
        self.win.bind("<Motion>", self._on_motion)
        self.win.bind("<Button-3>", self._on_right_click)
        self.win.bind("<Double-Button-1>", lambda e: self._show_main())

    def _make_tint(self, s, color):
        """生成状态色径向渐变 (中心实 -> 边缘透明) — 模拟玻璃的彩色光晕."""
        from PIL import Image, ImageTk
        r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
        im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        for y in range(s):
            for x in range(s):
                dx, dy = x - s//2, y - s//2
                d = (dx*dx + dy*dy) ** 0.5
                if d < s * 0.42:
                    a = int(100 * (1 - d / (s * 0.42)) ** 2.2)
                    if a > 0:
                        im.putpixel((x, y), (r, g, b, a))
        return ImageTk.PhotoImage(im)

    def _make_specular(self, s):
        """生成顶部月牙形 specular 高光 (白 -> 透明, 渐变)."""
        from PIL import Image, ImageTk
        im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        for y in range(s // 2 + 4):
            for x in range(s):
                dx = x - s//2
                # 月牙形: 中间宽, 边缘窄
                if abs(dx) < s * 0.32:
                    edge_fall = 1.0 - (abs(dx) / (s * 0.32)) ** 1.2
                    a = int(180 * edge_fall * max(0, 1 - y / (s // 2)) ** 1.5)
                    if a > 0:
                        im.putpixel((x, y), (255, 255, 255, a))
        return ImageTk.PhotoImage(im)

    def _current_state(self):
        """从 state[] 推导出当前视觉状态: 模式 / 颜色 / 是否脉冲."""
        c = self.c
        if not state["enabled"]:
            return ("empty", c["off"], False)
        if state["recording"]:
            return ("solid", c["rec"], True)
        return ("glass", c["accent"], False)

    def _redraw(self, force=False):
        """根据 state 切 4 个核心视觉元素: body / tint / specular / pulses."""
        mode, color, pulsing = self._current_state()
        sig = (mode, color, pulsing)
        if not force and sig == self._state_cache:
            return
        self._state_cache = sig
        c = self.c
        if mode == "empty":
            # 禁用: 极淡灰
            self.cv.itemconfigure(self._body, fill="#eef0f3", outline="")
            self.cv.itemconfigure(self._inner_shadow, outline="#e0e2e6", width=1)
        elif mode == "solid":
            # 录音: 柔和粉红
            self.cv.itemconfigure(self._body, fill="#fee2e2", outline="")
            self.cv.itemconfigure(self._inner_shadow, outline="#fca5a5", width=1)
        else:  # glass
            # 待命: 白底玻璃
            self.cv.itemconfigure(self._body, fill="#ffffff", outline="")
            self.cv.itemconfigure(self._inner_shadow, outline="#bfdbfe", width=1)
        # 切 tint (状态色光晕)
        if hasattr(self, "_glass_tints"):
            self.cv.itemconfigure(self._tint_id, image=self._glass_tints.get(mode))
        # Specular 高光 (待命时显示, 录音时减弱)
        self.cv.itemconfigure(self._specular_id,
                              state="normal" if mode != "solid" else "hidden")
        # rim light
        if mode == "empty":
            self.cv.itemconfigure(self._rim_top, state="hidden")
            self.cv.itemconfigure(self._rim_bottom, state="hidden")
        else:
            self.cv.itemconfigure(self._rim_top, state="normal")
            self.cv.itemconfigure(self._rim_bottom, state="normal")
        # 品牌图 vs 录音方块
        if hasattr(self, "_photo_id") and self._photo_id is not None:
            self.cv.itemconfigure(self._photo_id, state="hidden" if mode == "solid" else "normal")
        if hasattr(self, "_rec_dot"):
            self.cv.itemconfigure(self._rec_dot, state="normal" if mode == "solid" else "hidden")
        # 脉冲环
        for oval, _ in self._pulses:
            self.cv.itemconfigure(oval, state="normal" if pulsing else "hidden")

    def _animate(self):
        if not self.visible:
            return
        if state["recording"]:
            t = time.time()
            s = self.SIZE
            for i, (oval, phase_off) in enumerate(self._pulses):
                phase = (t * 1.2 + phase_off) % 1.0
                # 脉冲半径: 从主体边缘向外扩散
                base_r = s / 2 - 2  # 主体圆半径
                r = base_r + 8 * phase
                cx, cy = s / 2, s / 2
                self.cv.coords(oval, cx - r, cy - r, cx + r, cy + r)
                # 透明度: 越外越淡
                alpha = max(1, int(4 * (1.0 - phase)))
                self.cv.itemconfigure(oval, width=alpha)
        else:
            for oval, _ in self._pulses:
                self.cv.coords(oval, 0, 0, 0, 0)
        self._redraw()
        self.win.after(50, self._animate)

    def _trigger_press_anim(self):
        self._press_anim = [(0, 0.92), (60, 1.06), (160, 0.98), (220, 1.0)]
        self._press_anim_t = 0

    def _apply_scale(self, scale):
        s = self.SIZE
        cx, cy = s // 2, s // 2
        r = (s // 2 - 4) * scale
        self.cv.coords(self._body, cx - r, cy - r, cx + r, cy + r)

    # ─────────────── 拖动 / 点击 / 右键 ───────────────
    def _on_press(self, e):
        self._drag_data["x"] = e.x_root
        self._drag_data["y"] = e.y_root
        self._drag_data["moved"] = False
        self._pressed = True
        self._trigger_press_anim()

    def _on_motion(self, e):
        if not self._pressed:
            return
        dx = e.x_root - self._drag_data["x"]
        dy = e.y_root - self._drag_data["y"]
        if abs(dx) + abs(dy) > self.CLICK_THRESHOLD:
            self._drag_data["moved"] = True
        if not self._drag_data["moved"]:
            return
        x = self.win.winfo_x() + dx
        y = self.win.winfo_y() + dy
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max(0, min(x, sw - self.SIZE))
        y = max(0, min(y, sh - self.SIZE))
        self.win.geometry(f"+{x}+{y}")
        self._drag_data["x"] = e.x_root
        self._drag_data["y"] = e.y_root

    def _on_release(self, e):
        self._pressed = False
        x = self.win.winfo_x(); y = self.win.winfo_y()
        config["bubble_x"] = x; config["bubble_y"] = y
        save_config()
        if not self._drag_data["moved"]:
            self._show_main()

    def _on_right_click(self, e):
        c = self.c
        # 刷新菜单项
        self._menu.delete(0, tk.END)
        s = "已启用" if state["enabled"] else "已禁用"
        rec = " · 录音中" if state["recording"] else ""
        self._menu.add_command(label=f"言栖  ·  {s}{rec}", state=tk.DISABLED)
        self._menu.add_separator()
        toggle_label = "禁用功能" if state["enabled"] else "启用功能"
        self._menu.add_command(label=toggle_label, command=self._menu_toggle)
        self._menu.add_command(label="显示主窗口", command=self._show_main)
        self._menu.add_separator()
        self._menu.add_command(label="退出", command=self._menu_exit)
        try:
            self._menu.tk_popup(e.x_root, e.y_root)
        finally:
            self._menu.grab_release()

    def _menu_toggle(self):
        state["enabled"] = not state["enabled"]
        log(f"气泡菜单切换: {'启用' if state['enabled'] else '禁用'}")
        (sound_toggle_on if state["enabled"] else sound_toggle_off)()
        ui_queue.put(("toggled", state["enabled"]))
        if not state["enabled"] and state["recording"]:
            stop_recording()

    def _menu_exit(self):
        # 与 tray_exit 同款, 但从气泡触发
        state["enabled"] = False; state["recording"] = False
        orig = _mic_guard_state.get("orig_id")
        if orig:
            try:
                _policy_set_default(orig)
                _mic_guard_state["orig_id"] = None
                log("退出时已恢复默认麦克风")
            except Exception as e:
                log(f"退出时恢复麦克风失败: {e}")
        log("气泡退出")
        try: self.win.destroy()
        except Exception: pass
        os._exit(0)

    def _show_main(self):
        """显示主窗口, 隐藏气泡"""
        # 先解 withdraw 否则 deiconify 没效果
        try:
            self.mw.root.deiconify()
            self.mw.root.lift()
            self.mw.root.focus_force()
        except Exception as e:
            log(f"恢复主窗口失败: {e}")
        self.hide()

    # ─────────────── 显示 / 隐藏 ───────────────
    def show(self):
        if self.visible: return
        if not config.get("floating_bubble", True):
            return
        # 位置: 持久化坐标 > 默认 (屏幕偏右下角, 不贴边 — 给用户拖动缓冲)
        x = config.get("bubble_x")
        y = config.get("bubble_y")
        # 持久化坐标也必须在屏内
        if x is None or y is None or x < 0 or y < 0:
            x = None; y = None
        if x is None or y is None:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            x = sw - self.SIZE - 80
            y = sh - self.SIZE - 100
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max(0, min(x, sw - self.SIZE))
        y = max(0, min(y, sh - self.SIZE))
        self.win.geometry(f"+{x}+{y}")
        self.win.deiconify()
        self.visible = True
        self._state_cache = None
        self._redraw(force=True)
        self._animate()

    def hide(self):
        if not self.visible: return
        self.visible = False
        try: self.win.withdraw()
        except Exception: pass

    def destroy(self):
        try: self.win.destroy()
        except Exception: pass
        self.visible = False
        self.win = None



# ══════════
#  Switch 控件 (v0.6.1: Canvas 自绘, 替代丑 Checkbutton)
#  Why: tkinter Checkbutton 在浅色下渲染像个方块, 不像 iOS/macOS Switch.
#  用 Canvas 画胶囊+圆点, 点击切换 on/off, 带 100ms 缓出动效.
# ══════════
class Switch:
    W, H = 36, 20
    KNOB = 16

    def __init__(self, parent, variable, command=None, on_color=None, off_color=None, disabled=False):
        c = self._resolve_colors(parent, on_color, off_color)
        self._c = c
        self.var = variable
        self.command = command
        self.disabled = disabled
        self._anim_current = 1.0 if variable.get() else 0.0
        self._anim_target = self._anim_current
        self.cv = tk.Canvas(parent, width=self.W, height=self.H,
                            bg=parent.cget("bg"),
                            highlightthickness=0, cursor="hand2" if not disabled else "arrow")
        self._pill_l = self.cv.create_oval(0, 0, self.H, self.H, fill="", outline="")
        self._pill_r = self.cv.create_oval(self.W - self.H, 0, self.W, self.H, fill="", outline="")
        self._pill_m = self.cv.create_rectangle(
            self.H // 2, 0, self.W - self.H // 2, self.H, fill="", outline="")
        self._knob = self.cv.create_oval(
            2, 2, 2 + self.KNOB, 2 + self.KNOB, fill="#ffffff", outline="")
        self.cv.bind("<Button-1>", self._on_click)
        if not disabled:
            self.cv.after(16, self._animate)
        self._refresh()

    @staticmethod
    def _resolve_colors(parent, on_color, off_color):
        p = parent
        while p is not None:
            if hasattr(p, "c") and isinstance(getattr(p, "c"), dict):
                c = getattr(p, "c")
                return {
                    "on": on_color or c.get("accent", "#3b5bdb"),
                    "off": off_color or c.get("border2", "#d1d5db"),
                }
            p = getattr(p, "master", None)
        return {"on": on_color or "#3b5bdb", "off": off_color or "#d1d5db"}

    def _on_click(self, _event=None):
        if self.disabled: return
        self.var.set(not self.var.get())
        self._anim_target = 1.0 if self.var.get() else 0.0
        if self.command:
            try: self.command()
            except Exception as e: log("Switch command 失败: " + repr(e))

    def _animate(self):
        if abs(self._anim_current - self._anim_target) > 0.01:
            step = 0.18 if self._anim_target > self._anim_current else -0.18
            self._anim_current += step
            self._anim_current = max(0.0, min(1.0, self._anim_current))
            self._refresh()
        try:
            self.cv.after(16, self._animate)
        except Exception:
            pass

    def _refresh(self):
        c = self._c
        fill = c["on"] if self._anim_current > 0.5 else c["off"]
        self.cv.itemconfigure(self._pill_l, fill=fill)
        self.cv.itemconfigure(self._pill_r, fill=fill)
        self.cv.itemconfigure(self._pill_m, fill=fill)
        margin = 2
        max_off = margin
        max_on = self.W - self.KNOB - margin
        kx0 = max_off + self._anim_current * (max_on - max_off)
        kx1 = kx0 + self.KNOB
        self.cv.coords(self._knob, kx0, 2, kx1, 2 + self.KNOB)


# ══════════
#  AboutDialog (v0.6.1: 替代 messagebox.showinfo, 带品牌图)
# ══════════
class AboutDialog:
    def __init__(self, parent, main_win):
        self.mw = main_win
        c = main_win.c
        self.win = tk.Toplevel(parent)
        self.win.title("关于 言栖")
        self.win.resizable(False, False)
        self.win.configure(bg=c["bg"])
        _apply_window_glass(self.win, 0.985)
        self.win.transient(parent)
        self.win.attributes("-topmost", True)
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            w, h = 380, 380
            self.win.geometry(f"{w}x{h}+{px + (pw - w)//2}+{py + (ph - h)//2}")
        except Exception:
            self.win.geometry("380x380")
        self.win.grab_set()
        self._build()
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self):
        c = self.mw.c
        if _brand_img is not None:
            try:
                from PIL import ImageTk
                photo = ImageTk.PhotoImage(self._brand_resized(96))
                img_label = tk.Label(self.win, image=photo, bg=c["bg"])
                img_label.image = photo
                img_label.pack(pady=(28, 12))
            except Exception as e:
                log("About 品牌图加载失败: " + repr(e))
                tk.Label(self.win, text="言栖", font=("Microsoft YaHei UI", 28, "bold"),
                         fg=c["accent"], bg=c["bg"]).pack(pady=(40, 12))
        else:
            tk.Label(self.win, text="言栖", font=("Microsoft YaHei UI", 28, "bold"),
                     fg=c["accent"], bg=c["bg"]).pack(pady=(40, 12))
        tk.Label(self.win, text="言栖 v0.5.0 (pre-release)",
                 font=("Microsoft YaHei UI", 14, "bold"),
                 fg=c["fg"], bg=c["bg"]).pack()
        tk.Label(self.win, text="本地离线识别 · sherpa-onnx + SenseVoice",
                 font=("Microsoft YaHei UI", 9), fg=c["fg2"], bg=c["bg"]).pack(pady=(4, 0))
        features = [
            "多语种自动检测: 中文 / 英文 / 日文 / 韩文 / 粤语",
            "录音隐私: WASAPI 独占 + 切默认麦克风",
            "悬浮气泡: 状态一眼可见",
        ]
        for ft in features:
            tk.Label(self.win, text="·  " + ft,
                     font=("Microsoft YaHei UI", 9), fg=c["fg2"], bg=c["bg"]).pack(
                anchor=tk.W, padx=32, pady=2)
        link = tk.Label(self.win, text="github.com/Xinyang-S/STT-YanQi",
                        font=("Consolas", 9), fg=c["accent"], bg=c["bg"], cursor="hand2")
        link.pack(pady=(20, 0))
        close = tk.Button(self.win, text="关闭", font=("Microsoft YaHei UI", 9),
                          fg=c["btn_fg"], bg=c["accent"], activebackground=c["accent_active"],
                          activeforeground=c["btn_fg"], relief=tk.FLAT, cursor="hand2",
                          bd=0, highlightthickness=0, padx=24, pady=6,
                          command=self._on_close)
        close.pack(pady=(20, 20))

    def _brand_resized(self, size):
        from PIL import Image as PILImage
        img = _brand_img.copy()
        img.thumbnail((size, size), PILImage.LANCZOS)
        return img

    def _on_close(self):
        try: self.win.grab_release()
        except Exception: pass
        self.win.destroy()



# ═══════════════════════════════════════════════════════════
#  主界面 (v0.9.0 Liquid Glass)
# ═══════════════════════════════════════════════════════════
class MainWindow:
    """Liquid Glass main surface:
    - Canvas draws the background, floating glass plates, and soft refraction.
    - Tk widgets are placed as windows above those plates for interaction.
    - The visual system intentionally uses layered translucency, highlights,
      cool shadows, and state-colored liquid tints instead of flat cards.
    """
    _RB_SIZE = 216
    _RB_CX, _RB_CY = 108, 108
    _RB_R = 74

    def __init__(self, tray_ref, start_minimized=False):
        self.tray = tray_ref
        self.root = tk.Tk()
        self.root.title("言栖")
        # 窗口图标: frozen 模式从 _MEIPASS 取, 开发模式从工作目录取
        ico_path = _resolve_asset_path("app.ico")
        if ico_path and os.path.exists(ico_path):
            try: self.root.iconbitmap(default=ico_path)
            except Exception: pass
        # v0.9.0 Liquid Glass 调色板: 冷光背景 + 玻璃面板 + 系统强调色
        self.c = {
            "bg":            "#edf5fb",
            "bg2":           "#f8fcff",
            "card":          "#fbfdff",
            "glass":         "#f7fbff",
            "glass_panel":   "#f4faff",
            "glass_deep":    "#e7f2fb",
            "glass_border":  "#d2e4f4",
            "glass_rim":     "#ffffff",
            "border":        "#dfeaf4",
            "border2":       "#bfd2e3",
            "shadow":        "#d4e2ee",
            "shadow2":       "#bdccdc",
            "liquid_a":      "#d8f0ff",
            "liquid_b":      "#fff6df",
            "liquid_c":      "#e8ddff",
            "liquid_d":      "#dff9f0",
            "fg":            "#11243a",
            "fg2":           "#53667a",
            "fg3":           "#8a9aad",
            "accent":        "#007aff",
            "accent_active": "#0062cc",
            "rec":           "#ff3b30",
            "ok":            "#34c759",
            "warn":          "#ff9500",
            "off":           "#8e8e93",
            "err":           "#ff3b30",
            "btn_fg":        "#ffffff",
        }
        # 定位屏幕右下角
        sw = self.root.winfo_screenwidth(); sh = self.root.winfo_screenheight()
        w, h = 390, 640
        self.root.geometry(f"{w}x{h}+{sw - w - 60}+{sh - h - 100}")
        self.root.resizable(True, True); self.root.minsize(360, 560)
        self.root.configure(bg=self.c["bg"])
        _apply_window_glass(self.root, 0.985)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if start_minimized:
            self.root.withdraw()  # 开机启动时直接进托盘, 不弹主窗口
        # 动画状态 — v0.6.1 简化: 仅按下态 scale 缓动, 无装饰动效
        self._rec_btn_pressed = False
        self._press_scale = 1.0  # 按下态 scale, 1.0 = 无
        self._is_placeholder = True
        self._settings_win = None
        self.bubble = None
        self._build(); self._poll(); self._animate()
        # 品牌图加载完后用一次
        if _brand_img is not None:
            self._refresh_brand()
        # 悬浮气泡 (默认开启, 默认不显示)
        self.bubble = FloatingBubble(self)
        # 启动时若 minimized (开机启动), 自动显示气泡
        if start_minimized and config.get("floating_bubble", True):
            self.root.after(200, self.bubble.show)

    def _brand_resized(self, size):
        """从 _brand_img 拷贝并 resize 到 size x size (RGBA)."""
        from PIL import Image as PILImage
        img = _brand_img.copy()
        img.thumbnail((size, size), PILImage.LANCZOS)
        return img

    def _pil_to_photo(self, pil_img):
        """PIL Image -> tk PhotoImage (用 ImageTk 桥接)."""
        try:
            from PIL import ImageTk
            return ImageTk.PhotoImage(pil_img)
        except Exception as e:
            log(f"PIL->PhotoImage 失败: {e!r}")
            return None

    def _refresh_brand(self):
        """主窗口品牌图已就绪后调用 — 把录音按钮中心的占位文字替换为品牌图."""
        if not hasattr(self, "rbtn_cv"): return
        c = self.c
        # 删掉 rbtn_cv 里之前的占位文字 (如果有)
        if hasattr(self, "_rbtn_placeholder_text"):
            try: self.rbtn_cv.delete(self._rbtn_placeholder_text)
            except Exception: pass
            self._rbtn_placeholder_text = None
        if getattr(self, "_rbtn_photo_id", None) is not None:
            try: self.rbtn_cv.delete(self._rbtn_photo_id)
            except Exception: pass
            self._rbtn_photo_id = None
        # 加品牌图
        photo = self._pil_to_photo(self._brand_resized(92))
        if photo is None: return
        self._rbtn_photo_ref = photo  # 保活, 防被 GC
        self._rbtn_photo_id = self.rbtn_cv.create_image(
            self._RB_CX, self._RB_CY, image=photo)
        # 把 ■ 移到最上层 (录音态时显示)
        if hasattr(self, "_rec_square_id"):
            self.rbtn_cv.tag_raise(self._rec_square_id)

    def _on_close(self):
        """点击 X 关闭主窗口: 隐藏主窗口, 视设置决定是否显示悬浮气泡."""
        self.root.withdraw()
        # 关闭设置窗口 (避免遮罩残留)
        if self._settings_win is not None:
            try: self._settings_win.destroy()
            except Exception: pass
            self._settings_win = None
        if config.get("floating_bubble", True) and self.bubble is not None:
            self.bubble.show()

    # ─────────────── Liquid Glass 绘制工具 ───────────────
    def _canvas_round_rect(self, cv, x1, y1, x2, y2, r, fill, outline="", width=1, tags=()):
        pts = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1
        ]
        return cv.create_polygon(
            pts, smooth=True, splinesteps=18, fill=fill,
            outline=outline, width=width, tags=tags
        )

    def _make_liquid_bg(self, w, h):
        from PIL import ImageFilter, ImageTk
        w = max(2, int(w)); h = max(2, int(h))
        im = Image.new("RGB", (w, h), self.c["bg"])
        px = im.load()
        top = (244, 250, 255); bottom = (228, 240, 249)
        for y in range(h):
            t = y / max(1, h - 1)
            row = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
            for x in range(w):
                px[x, y] = row

        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay, "RGBA")
        blobs = [
            (-0.18*w, 0.04*h, 0.54*w, 0.34*h, (120, 210, 255, 98)),
            (0.52*w, 0.02*h, 1.20*w, 0.34*h, (255, 224, 150, 80)),
            (0.56*w, 0.35*h, 1.16*w, 0.74*h, (178, 154, 255, 58)),
            (-0.18*w, 0.56*h, 0.50*w, 1.06*h, (130, 242, 214, 70)),
            (0.18*w, 0.20*h, 0.86*w, 0.64*h, (255, 255, 255, 82)),
        ]
        for x1, y1, x2, y2, color in blobs:
            d.ellipse([x1, y1, x2, y2], fill=color)
        overlay = overlay.filter(ImageFilter.GaussianBlur(radius=max(18, int(w * 0.07))))
        im = Image.alpha_composite(im.convert("RGBA"), overlay)

        grain = Image.new("RGBA", (w, h), (255, 255, 255, 0))
        gd = ImageDraw.Draw(grain, "RGBA")
        step = 6
        for y in range(0, h, step):
            for x in range((y // step) % 2 * 3, w, step * 2):
                gd.point((x, y), fill=(255, 255, 255, 18))
        im = Image.alpha_composite(im, grain)
        return ImageTk.PhotoImage(im)

    def _draw_liquid_card(self, x, y, w, h, radius=32, tint=None):
        c = self.c; cv = self.bg_canvas
        tag = "liquid_draw"
        tint = tint or c["glass_panel"]
        self._canvas_round_rect(cv, x + 2, y + 8, x + w + 2, y + h + 12, radius,
                                c["shadow"], outline="", tags=(tag,))
        self._canvas_round_rect(cv, x, y, x + w, y + h, radius,
                                tint, outline=c["glass_border"], width=1, tags=(tag,))
        self._canvas_round_rect(cv, x + 7, y + 7, x + w - 7, y + h - 7, max(12, radius - 8),
                                "#fbfdff", outline="", width=0, tags=(tag,))
        cv.create_arc(x + 16, y + 12, x + w - 16, y + h * 0.72,
                      start=196, extent=146, style=tk.ARC,
                      outline=c["glass_rim"], width=2, tags=(tag,))
        cv.create_arc(x + 10, y + h * 0.42, x + w - 10, y + h + 10,
                      start=24, extent=132, style=tk.ARC,
                      outline=c["border2"], width=1, tags=(tag,))

    def _layout_shell(self, _event=None):
        if not hasattr(self, "bg_canvas"):
            return
        cv = self.bg_canvas
        w = max(1, cv.winfo_width()); h = max(1, cv.winfo_height())
        cv.delete("liquid_draw")
        if getattr(self, "_bg_size", None) != (w, h):
            self._bg_photo = self._make_liquid_bg(w, h)
            self._bg_size = (w, h)
        cv.create_image(0, 0, image=self._bg_photo, anchor=tk.NW, tags=("liquid_draw",))
        self._draw_window_chrome(w, h)

        m = max(16, min(24, int(w * 0.052)))
        nav_h = 58
        nav_y = 14
        nav_w = w - m * 2
        toolbar_h = 52
        toolbar_y = h - toolbar_h - 14
        card_y = nav_y + nav_h + 14
        card_h = min(304, max(248, int(h * 0.43)))
        result_y = card_y + card_h + 14
        result_h = toolbar_y - result_y - 14
        if result_h < 128:
            card_h = max(232, card_h - (128 - result_h))
            result_y = card_y + card_h + 14
            result_h = max(112, toolbar_y - result_y - 14)
        card_w = w - m * 2

        self._draw_liquid_card(m, nav_y, nav_w, nav_h, radius=26, tint=self.c["glass"])
        self._draw_liquid_card(m, card_y, card_w, card_h, radius=38, tint=self.c["glass_panel"])
        self._draw_liquid_card(m, result_y, card_w, result_h, radius=28, tint=self.c["card"])
        self._draw_liquid_card(m, toolbar_y, card_w, toolbar_h, radius=26, tint=self.c["glass"])
        cv.tag_lower("liquid_draw")

        cv.coords(self._nav_window, m + 16, nav_y + 10)
        cv.itemconfigure(self._nav_window, width=nav_w - 32, height=nav_h - 18)
        orb_y = card_y + card_h * 0.43
        cv.coords(self._rbtn_window, w / 2, orb_y)
        cv.coords(self._status_main_window, w / 2, card_y + card_h - 64)
        cv.coords(self._status_sub_window, w / 2, card_y + card_h - 36)
        cv.coords(self._hint_window, w / 2, card_y + card_h - 14)
        cv.coords(self._result_title_window, m + 20, result_y + 20)
        cv.coords(self._result_window, m + 13, result_y + 40)
        cv.itemconfigure(self._result_window, width=card_w - 26, height=max(70, result_h - 54))
        cv.coords(self._toolbar_window, m + 16, toolbar_y + 9)
        cv.itemconfigure(self._toolbar_window, width=card_w - 32, height=toolbar_h - 18)

    def _draw_window_chrome(self, w, h):
        cv = self.bg_canvas
        c = self.c
        tag = "liquid_draw"
        self._canvas_round_rect(cv, 5, 5, w - 5, h - 5, 34,
                                "", outline="#ffffff", width=1, tags=(tag,))
        self._canvas_round_rect(cv, 7, 7, w - 7, h - 7, 32,
                                "", outline=c["glass_border"], width=1, tags=(tag,))
        cv.create_line(34, 8, w - 34, 8, fill="#ffffff", width=1, tags=(tag,))
        cv.create_line(24, h - 8, w - 24, h - 8, fill=c["border2"], width=1, tags=(tag,))

    # ─────────────── 构建 UI ───────────────
    def _build(self):
        c = self.c
        self._engine_text = "本地 SenseVoice"
        self.bg_canvas = tk.Canvas(self.root, bg=c["bg"], highlightthickness=0, bd=0)
        self.bg_canvas.pack(fill=tk.BOTH, expand=True)

        self.nav_frame = tk.Frame(self.bg_canvas, bg=c["glass"])
        left = tk.Frame(self.nav_frame, bg=c["glass"])
        left.pack(side=tk.LEFT, fill=tk.Y)
        tk.Label(left, text="言栖", font=("Microsoft YaHei UI", 15, "bold"),
                 fg=c["fg"], bg=c["glass"]).pack(anchor=tk.W)
        tk.Label(left, text="液态语音输入", font=("Microsoft YaHei UI", 8),
                 fg=c["fg3"], bg=c["glass"]).pack(anchor=tk.W, pady=(1, 0))
        right = tk.Frame(self.nav_frame, bg=c["glass"])
        right.pack(side=tk.RIGHT, fill=tk.Y)
        self.mode_lbl = tk.Label(right, text="", font=("Microsoft YaHei UI", 8),
                                 fg=c["fg3"], bg=c["glass"])
        self.mode_lbl.pack(side=tk.RIGHT, padx=(8, 0), pady=(8, 0))
        self.tgl_lbl = tk.Label(right, text="启用", font=("Microsoft YaHei UI", 9, "bold"),
                                fg=c["accent"], bg=c["glass"], cursor="hand2", padx=8, pady=4)
        self.tgl_lbl.pack(side=tk.RIGHT, pady=(5, 0))
        self.tgl_lbl.bind("<Button-1>", lambda e: self._toggle())
        self._nav_window = self.bg_canvas.create_window(0, 0, window=self.nav_frame, anchor=tk.NW)

        self.rbtn_cv = tk.Canvas(self.bg_canvas, width=self._RB_SIZE, height=self._RB_SIZE,
                                 bg=c["glass_panel"], highlightthickness=0, cursor="hand2")
        self._build_record_button()
        self._rbtn_window = self.bg_canvas.create_window(0, 0, window=self.rbtn_cv, anchor=tk.CENTER)

        self.lbl_main = tk.Label(self.bg_canvas, text="待命",
                                 font=("Microsoft YaHei UI", 22, "bold"),
                                 fg=c["fg"], bg=c["glass_panel"])
        self.lbl_sub = tk.Label(self.bg_canvas, text="按住  Right Ctrl  开始录音",
                                font=("Microsoft YaHei UI", 9), fg=c["fg2"], bg=c["glass_panel"])
        self.hint_lbl = tk.Label(self.bg_canvas, text="本地离线 · 私密输入",
                                 font=("Microsoft YaHei UI", 8), fg=c["fg3"], bg=c["glass_panel"])
        self._status_main_window = self.bg_canvas.create_window(0, 0, window=self.lbl_main, anchor=tk.CENTER)
        self._status_sub_window = self.bg_canvas.create_window(0, 0, window=self.lbl_sub, anchor=tk.CENTER)
        self._hint_window = self.bg_canvas.create_window(0, 0, window=self.hint_lbl, anchor=tk.CENTER)

        self.result_title = tk.Label(self.bg_canvas, text="识别结果",
                                     font=("Microsoft YaHei UI", 9, "bold"),
                                     fg=c["fg2"], bg=c["card"])
        self._result_title_window = self.bg_canvas.create_window(0, 0, window=self.result_title, anchor=tk.W)
        text_container = tk.Frame(self.bg_canvas, bg=c["card"], bd=0)
        self.txt = tk.Text(text_container, font=("Microsoft YaHei UI", 12),
                           fg=c["fg"], bg=c["card"], relief=tk.FLAT,
                           wrap=tk.WORD, borderwidth=0, padx=12, pady=8,
                           insertbackground=c["accent"], spacing1=2, spacing3=2,
                           height=4, takefocus=0)
        scroll = tk.Scrollbar(text_container, command=self.txt.yview, bd=0,
                              bg=c["card"], troughcolor=c["card"],
                              activebackground=c["glass_border"], width=4)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.txt.configure(yscrollcommand=scroll.set)
        self.txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._result_window = self.bg_canvas.create_window(0, 0, window=text_container, anchor=tk.NW)

        self.toolbar_frame = tk.Frame(self.bg_canvas, bg=c["glass"])
        tk.Label(self.toolbar_frame, text="v0.5.0", font=("Cascadia Mono", 8),
                 fg=c["fg3"], bg=c["glass"]).pack(side=tk.LEFT, pady=4)
        self.copy_btn = self._mk_ghost(self.toolbar_frame, "复制", self._copy_result)
        self.copy_btn.pack(side=tk.RIGHT, padx=(12, 0), pady=4)
        self._mk_ghost(self.toolbar_frame, "设置",
                       lambda: SettingsDialog(self.root, self)).pack(side=tk.RIGHT, padx=(12, 0), pady=4)
        self._mk_ghost(self.toolbar_frame, "关于", self._about).pack(side=tk.RIGHT, padx=(12, 0), pady=4)
        self._toolbar_window = self.bg_canvas.create_window(0, 0, window=self.toolbar_frame, anchor=tk.NW)

        self.rbtn_cv.bind("<ButtonPress-1>", lambda e: self._on_press())
        self.rbtn_cv.bind("<ButtonRelease-1>", lambda e: self._on_release())
        self.root.bind("<ButtonRelease-1>", self._root_release, add="+")
        self.txt.bind("<Double-Button-1>", self._copy_result)
        self._text_menu = tk.Menu(self.root, tearoff=0,
                                   font=("Microsoft YaHei UI", 9),
                                   bg=c["card"], fg=c["fg"],
                                   activebackground=c["accent"],
                                   activeforeground="#ffffff",
                                   relief=tk.FLAT, bd=1)
        self._text_menu.add_command(label="复制", command=self._copy_result)
        self._text_menu.add_command(label="全选", command=self._select_all_result)
        self.txt.bind("<Button-3>", self._show_text_menu)
        self.root.bind_all("<Control-c>", lambda e: self._copy_result())
        self.txt.tag_configure("title", font=("Microsoft YaHei UI", 13, "bold"),
                               foreground=c["fg2"], spacing1=2, spacing3=2)
        self.txt.tag_configure("kbd", font=("Cascadia Mono", 9, "bold"),
                               foreground=c["accent"], background="#e6f1ff", spacing1=0, spacing3=0)
        self.txt.tag_configure("result", font=("Microsoft YaHei UI", 12),
                               foreground=c["fg"], lmargin1=2, lmargin2=2)
        self.txt.tag_configure("error", font=("Microsoft YaHei UI", 11),
                               foreground=c["rec"])
        self.txt.configure(state=tk.DISABLED)
        def _on_text_wheel(e):
            if getattr(e, "num", None) == 4: delta = -1
            elif getattr(e, "num", None) == 5: delta = 1
            else: delta = -1 if e.delta > 0 else 1
            self.txt.yview_scroll(delta, "units")
        self.txt.bind("<Enter>", lambda e: self.txt.bind_all("<MouseWheel>", _on_text_wheel))
        self.txt.bind("<Leave>", lambda e: self.txt.unbind_all("<MouseWheel>"))
        self.bg_canvas.bind("<Configure>", self._layout_shell)
        self.root.after_idle(self._set_text_placeholder)
        self.root.after_idle(self._layout_shell)

    def _build_record_button(self):
        c = self.c; cv = self.rbtn_cv; s = self._RB_SIZE
        self._rbtn_ring1_id = cv.create_oval(12, 12, s - 12, s - 12,
                                             outline="#ffffff", width=1)
        self._rbtn_ring2_id = cv.create_oval(24, 24, s - 24, s - 24,
                                             outline=c["liquid_a"], width=3)
        self._rbtn_shadow_outer_id = cv.create_oval(
            self._RB_CX - self._RB_R - 10, self._RB_CY - self._RB_R + 10,
            self._RB_CX + self._RB_R + 10, self._RB_CY + self._RB_R + 20,
            fill=c["shadow"], outline="")
        self._rbtn_shadow_inner_id = cv.create_oval(
            self._RB_CX - self._RB_R - 4, self._RB_CY - self._RB_R + 5,
            self._RB_CX + self._RB_R + 4, self._RB_CY + self._RB_R + 10,
            fill=c["shadow2"], outline="")
        self._rbtn_circle_id = cv.create_oval(
            self._RB_CX - self._RB_R, self._RB_CY - self._RB_R,
            self._RB_CX + self._RB_R, self._RB_CY + self._RB_R,
            fill=c["card"], outline=c["glass_border"], width=2)
        self._rbtn_tint_id = cv.create_oval(
            self._RB_CX - 50, self._RB_CY - 54, self._RB_CX + 52, self._RB_CY + 48,
            fill=c["liquid_a"], outline="")
        self._rbtn_highlight_id = cv.create_arc(
            self._RB_CX - self._RB_R + 14, self._RB_CY - self._RB_R + 10,
            self._RB_CX + self._RB_R - 14, self._RB_CY + self._RB_R - 20,
            start=198, extent=144, style=tk.ARC, outline="#ffffff", width=3)
        self._rec_square_id = cv.create_rectangle(
            self._RB_CX - 18, self._RB_CY - 18,
            self._RB_CX + 18, self._RB_CY + 18,
            fill=c["btn_fg"], outline="")
        cv.itemconfigure(self._rec_square_id, state="hidden")
        self._rbtn_photo_id = None
        self._rbtn_photo_ref = None
        self._rbtn_placeholder_text = None
        if _brand_img is not None:
            self._refresh_brand()
        else:
            self._rbtn_placeholder_text = cv.create_text(
                self._RB_CX, self._RB_CY, text="◐",
                font=("Segoe UI Symbol", 52), fill=c["accent"])

    def _mk_ghost(self, parent, text, cmd):
        c = self.c
        surface = parent.cget("bg")
        def on_enter(_e): btn.configure(fg=c["accent"])
        def on_leave(_e): btn.configure(fg=c["fg2"])
        btn = tk.Label(parent, text=text, font=("Microsoft YaHei UI", 9, "bold"),
                       fg=c["fg2"], bg=surface, cursor="hand2", padx=4, pady=2)
        btn.bind("<Button-1>", lambda e: cmd())
        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)
        return btn

    def _set_text_placeholder(self):
        self._is_placeholder = True
        self.txt.configure(state=tk.NORMAL)
        self.txt.delete("1.0", tk.END)
        self.txt.insert("1.0", "等待语音输入", "title")
        self.txt.insert("end", "\n\n按住  ")
        self.txt.insert("end", "Right Ctrl", "kbd")
        self.txt.insert("end", "  说话")
        self.txt.insert("end", "\n切换  ")
        self.txt.insert("end", "Ctrl + Shift + F9", "kbd")
        self.txt.insert("end", "  启用 / 禁用\n引擎  ")
        self.txt.insert("end", self._engine_text, "kbd")
        self.txt.configure(state=tk.DISABLED)

    # ─────────────── 录音按钮交互 ───────────────
    def _on_press(self):
        if not self._rec_btn_pressed and state["enabled"]:
            self._rec_btn_pressed = True
            self._press_scale = 0.97
            self._set_rec_visual(True)
            start_recording()

    def _on_release(self):
        if self._rec_btn_pressed:
            self._rec_btn_pressed = False
            self._press_scale = 1.0
            self._set_rec_visual(False)
            stop_recording()

    def _root_release(self, event):
        if self._rec_btn_pressed and event.widget is not self.rbtn_cv:
            self._rec_btn_pressed = False
            self._set_rec_visual(False)
            stop_recording()

    def _set_rec_visual(self, on):
        """Switch the liquid control orb between idle and recording material."""
        c = self.c
        if on:
            self.rbtn_cv.itemconfigure(self._rbtn_shadow_outer_id, fill="#ffd7d4")
            self.rbtn_cv.itemconfigure(self._rbtn_shadow_inner_id, fill="#ffb8b3")
            self.rbtn_cv.itemconfigure(self._rbtn_circle_id, fill=c["rec"], outline="")
            self.rbtn_cv.itemconfigure(self._rbtn_tint_id, fill="#ff9c96")
            self.rbtn_cv.itemconfigure(self._rbtn_ring1_id, outline="#fff0ee", width=2)
            self.rbtn_cv.itemconfigure(self._rbtn_ring2_id, outline="#ffb8b3", width=4)
            self.rbtn_cv.itemconfigure(self._rbtn_highlight_id, state="hidden")
            self.rbtn_cv.itemconfigure(self._rec_square_id, state="normal")
            if self._rbtn_photo_id is not None:
                self.rbtn_cv.itemconfigure(self._rbtn_photo_id, state="hidden")
            if self._rbtn_placeholder_text is not None:
                self.rbtn_cv.itemconfigure(self._rbtn_placeholder_text, state="hidden")
        else:
            self.rbtn_cv.itemconfigure(self._rbtn_shadow_outer_id, fill=c["shadow"])
            self.rbtn_cv.itemconfigure(self._rbtn_shadow_inner_id, fill=c["shadow2"])
            self.rbtn_cv.itemconfigure(self._rbtn_circle_id, fill=c["card"], outline=c["glass_border"])
            self.rbtn_cv.itemconfigure(self._rbtn_tint_id, fill=c["liquid_a"])
            self.rbtn_cv.itemconfigure(self._rbtn_ring1_id, outline="#ffffff", width=1)
            self.rbtn_cv.itemconfigure(self._rbtn_ring2_id, outline=c["liquid_a"], width=3)
            self.rbtn_cv.itemconfigure(self._rbtn_highlight_id, state="normal")
            self.rbtn_cv.itemconfigure(self._rec_square_id, state="hidden")
            if self._rbtn_photo_id is not None:
                self.rbtn_cv.itemconfigure(self._rbtn_photo_id, state="normal")
            if self._rbtn_placeholder_text is not None:
                self.rbtn_cv.itemconfigure(self._rbtn_placeholder_text, state="normal")

    def _animate(self):
        """Keep the liquid control responsive without adding visual noise."""
        if self._rec_btn_pressed and self._press_scale > 0.97:
            self._press_scale = max(0.97, self._press_scale - 0.04)
            self._apply_press_scale(self._press_scale)
        elif not self._rec_btn_pressed and self._press_scale < 1.0:
            self._press_scale = min(1.0, self._press_scale + 0.05)
            self._apply_press_scale(self._press_scale)
        if hasattr(self, "rbtn_cv") and state.get("recording"):
            phase = (time.time() * 1.45) % 1.0
            r1 = self._RB_R + 12 + 18 * phase
            r2 = self._RB_R + 30 - 10 * phase
            self.rbtn_cv.coords(self._rbtn_ring1_id,
                                self._RB_CX - r1, self._RB_CY - r1,
                                self._RB_CX + r1, self._RB_CY + r1)
            self.rbtn_cv.coords(self._rbtn_ring2_id,
                                self._RB_CX - r2, self._RB_CY - r2,
                                self._RB_CX + r2, self._RB_CY + r2)
        elif hasattr(self, "rbtn_cv"):
            self.rbtn_cv.coords(self._rbtn_ring1_id, 12, 12, self._RB_SIZE - 12, self._RB_SIZE - 12)
            self.rbtn_cv.coords(self._rbtn_ring2_id, 24, 24, self._RB_SIZE - 24, self._RB_SIZE - 24)
        self.root.after(40, self._animate)

    def _apply_press_scale(self, scale):
        r = self._RB_R * scale
        ring1 = r + 34
        ring2 = r + 21
        self.rbtn_cv.coords(self._rbtn_ring1_id,
                            self._RB_CX - ring1, self._RB_CY - ring1,
                            self._RB_CX + ring1, self._RB_CY + ring1)
        self.rbtn_cv.coords(self._rbtn_ring2_id,
                            self._RB_CX - ring2, self._RB_CY - ring2,
                            self._RB_CX + ring2, self._RB_CY + ring2)
        self.rbtn_cv.coords(self._rbtn_shadow_outer_id,
                            self._RB_CX - r - 8, self._RB_CY - r + 8,
                            self._RB_CX + r + 8, self._RB_CY + r + 16)
        self.rbtn_cv.coords(self._rbtn_shadow_inner_id,
                            self._RB_CX - r - 2, self._RB_CY - r + 4,
                            self._RB_CX + r + 2, self._RB_CY + r + 8)
        self.rbtn_cv.coords(self._rbtn_circle_id,
                            self._RB_CX - r, self._RB_CY - r,
                            self._RB_CX + r, self._RB_CY + r)
        self.rbtn_cv.coords(self._rbtn_tint_id,
                            self._RB_CX - r * 0.68, self._RB_CY - r * 0.72,
                            self._RB_CX + r * 0.70, self._RB_CY + r * 0.64)
        self.rbtn_cv.coords(self._rbtn_highlight_id,
                            self._RB_CX - r + 14, self._RB_CY - r + 12,
                            self._RB_CX + r - 14, self._RB_CY + r - 20)

    def _toggle(self):
        state["enabled"] = not state["enabled"]
        (sound_toggle_on if state["enabled"] else sound_toggle_off)()
        self._refresh()
        if not state["enabled"] and state["recording"]:
            stop_recording()
            self._on_release()

    def _refresh(self):
        c = self.c
        # 模式徽章 (极简: 独占 / 共享)
        mode = state.get("audio_mode", "共享")
        guarded = state.get("mic_guarded", False)
        mtext = ""
        if "独占" in mode: mtext += "独占"
        if guarded: mtext += " · 麦克风独占" if mtext else "麦克风独占"
        self.mode_lbl.configure(text=mtext)
        # 状态文字 + 顶栏开关
        if state["recording"]:
            self.lbl_main.configure(text="正在录音", fg=c["rec"])
            self.lbl_sub.configure(text="松开  Right Ctrl  结束并识别", fg=c["fg2"])
            self.hint_lbl.configure(text="液态隔离 · 录音中", fg=c["rec"])
            self.tgl_lbl.configure(text="停止", fg=c["rec"])
            self._set_rec_visual(True)
        elif state["enabled"]:
            self.lbl_main.configure(text="待命", fg=c["fg"])
            self.lbl_sub.configure(text="按住  Right Ctrl  开始录音", fg=c["fg2"])
            self.hint_lbl.configure(text="本地离线 · 私密输入", fg=c["fg3"])
            self.tgl_lbl.configure(text="启用", fg=c["accent"])
            self._set_rec_visual(False)
        else:
            self.lbl_main.configure(text="已禁用", fg=c["fg3"])
            self.lbl_sub.configure(text="点击右上角启用恢复  ·  Ctrl+Shift+F9", fg=c["fg3"])
            self.hint_lbl.configure(text="控制已暂停", fg=c["fg3"])
            self.tgl_lbl.configure(text="已禁用", fg=c["fg3"])
            self._set_rec_visual(False)

    def _poll(self):
        try:
            while True:
                m = ui_queue.get_nowait()
                k = m[0]
                if k == "recording": self._refresh(); self._update_tray()
                elif k == "result":
                    self._is_placeholder = False
                    self.txt.configure(state=tk.NORMAL)
                    self.txt.delete("1.0", tk.END)
                    self.txt.insert("1.0", m[1], "result")
                    self.txt.configure(state=tk.DISABLED)
                elif k == "error":
                    self._is_placeholder = False
                    self.txt.configure(state=tk.NORMAL)
                    self.txt.delete("1.0", tk.END)
                    self.txt.insert("1.0", "⚠  " + m[1], "error")
                    self.txt.configure(state=tk.DISABLED)
                elif k == "toggled": self._refresh(); self._update_tray()
                elif k == "show":
                    self.root.deiconify(); self.root.lift()
                    if self.bubble is not None and self.bubble.visible:
                        self.bubble.hide()
                elif k == "status": self._refresh()
        except queue_mod.Empty:
            pass
        self.root.after(100, self._poll)

    def _copy_result(self, _evt=None):
        # Text 设为 state=DISABLED 阻止输入, 但也阻止了 get().
        # 临时切到 NORMAL, 复制, 再切回.
        try:
            self.txt.configure(state=tk.NORMAL)
            content = self.txt.get("1.0", tk.END).strip()
            self.txt.configure(state=tk.DISABLED)
            if content and not self._is_placeholder:
                pyperclip.copy(content)
                # 底部 "复制" 按钮短暂反馈
                if hasattr(self, "copy_btn"):
                    old_text = self.copy_btn.cget("text")
                    self.copy_btn.configure(text="已复制 ✓", fg=self.c["ok"])
                    self.root.after(1500, lambda: self.copy_btn.configure(text=old_text, fg=self.c["fg2"]))
                log(f"复制结果: {content[:30]}...")
        except Exception as e:
            log(f"复制失败: {e}")

    def _select_all_result(self):
        # 切换到 NORMAL 才能操作 selection, 复制完再切回
        try:
            self.txt.configure(state=tk.NORMAL)
            self.txt.tag_add("sel", "1.0", "end")
            self.txt.mark_set("insert", "1.0")
            self.txt.see("insert")
            self.txt.configure(state=tk.DISABLED)
        except Exception as e:
            log(f"全选失败: {e}")

    def _show_text_menu(self, e):
        try:
            self._text_menu.tk_popup(e.x_root, e.y_root)
        finally:
            self._text_menu.grab_release()

    def _update_tray(self):
        t = getattr(self, 'tray', None)
        if t is None: return
        try:
            t.icon = get_tray_icon()
        except Exception as e:
            log(f"托盘图标更新失败: {e}")

    def _about(self):
        AboutDialog(self.root, self)

# ═══════════════════════════════════════════════════════════
class SettingsDialog:
    def __init__(self, parent, main_win):
        self.mw = main_win
        c = main_win.c
        if hasattr(main_win, "_settings_win") and main_win._settings_win is not None:
            try:
                main_win._settings_win.lift(); main_win._settings_win.focus_force()
                return
            except Exception: pass
        self.win = tk.Toplevel(parent)
        self.win.title("设置 — 言栖")
        self.win.resizable(False, False)
        self.win.configure(bg=c["bg"])
        _apply_window_glass(self.win, 0.985)
        self.win.transient(parent)
        self.win.attributes("-topmost", True)
        main_win._settings_win = self.win
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw = parent.winfo_width()
            w, h = 480, 540
            self.win.geometry(f"{w}x{h}+{px + pw + 8}+{py}")
        except Exception:
            self.win.geometry("480x540")
        self.win.update_idletasks()
        self.win.grab_set()
        style = ttk.Style()
        try: style.theme_use("clam")
        except Exception: pass
        style.configure("Glass.TNotebook", background=c["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
        # 选中 tab: 白底 + 蓝字 + 大号字 + 大 padding (放大效果)
        # 未选中 tab: 同 bg 色 + 灰字 + 9pt 小 padding
        style.configure("Glass.TNotebook.Tab",
                        background=c["bg"], foreground=c["fg3"],
                        padding=(18, 6), font=("Microsoft YaHei UI", 9),
                        borderwidth=0)
        style.map("Glass.TNotebook.Tab",
                  background=[("selected", c["card"]), ("active", c["bg"])],
                  foreground=[("selected", c["accent"]), ("active", c["fg2"])],
                  font=[("selected", ("Microsoft YaHei UI", 10, "bold")),
                         ("active", ("Microsoft YaHei UI", 9))],
                  padding=[("selected", (18, 10)), ("active", (18, 6))])
        style.configure("TFrame", background=c["bg"])
        style.configure("TLabel", background=c["bg"], foreground=c["fg"])
        tk.Frame(self.win, bg=c["glass_border"], height=1).pack(side=tk.TOP, fill=tk.X, padx=20, pady=(16, 0))
        nb = ttk.Notebook(self.win, style="Glass.TNotebook")
        nb.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 0))
        nb.add(self._general_tab(nb), text="  通用  ")
        nb.add(self._audio_tab(nb), text="  音频设备  ")
        tk.Frame(self.win, bg=c["glass_border"], height=1).pack(side=tk.TOP, fill=tk.X, padx=20, pady=(0, 0))
        bot = tk.Frame(self.win, bg=c["bg"])
        bot.pack(side=tk.TOP, fill=tk.X, padx=20, pady=10)
        tk.Label(bot, text="设置修改后立即生效", font=("Microsoft YaHei UI", 8),
                 fg=c["fg3"], bg=c["bg"]).pack(side=tk.LEFT)
        close_btn = tk.Button(bot, text="完成", font=("Microsoft YaHei UI", 9, "bold"),
                              fg=c["btn_fg"], bg=c["accent"], activebackground=c["accent_active"],
                              activeforeground=c["btn_fg"], relief=tk.FLAT, cursor="hand2",
                              bd=0, highlightthickness=0, padx=18, pady=4,
                              command=self._on_close)
        close_btn.pack(side=tk.RIGHT)

    def _on_close(self):
        try: self.win.grab_release()
        except Exception: pass
        self.mw._settings_win = None
        self.win.destroy()

    def _section_label(self, parent, text, hint=""):
        """分组小标题: 浅色无圆点, 配下方小灰字提示"""
        c = self.mw.c
        f = tk.Frame(parent, bg=c["bg"])
        f.pack(fill=tk.X, pady=(14, 6), anchor=tk.W)
        tk.Label(f, text=text, font=("Microsoft YaHei UI", 11, "bold"),
                 fg=c["fg"], bg=c["bg"], anchor=tk.W).pack(side=tk.LEFT)
        if hint:
            tk.Label(f, text="  " + hint, font=("Microsoft YaHei UI", 8),
                     fg=c["fg3"], bg=c["bg"]).pack(side=tk.LEFT)

    def _scrollable_tab(self, parent, attr_prefix):
        """Build an isolated notebook tab page with a canvas-backed scroll area."""
        c = self.mw.c
        page = tk.Frame(parent, bg=c["bg"])
        cv = tk.Canvas(page, bg=c["bg"], highlightthickness=0, bd=0)
        sb = tk.Scrollbar(page, orient=tk.VERTICAL, command=cv.yview,
                          bg=c["card"], troughcolor=c["bg"], width=6,
                          activebackground=c["glass_border"])
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        body = tk.Frame(cv, bg=c["bg"])
        window_id = cv.create_window((0, 0), window=body, anchor=tk.NW)
        setattr(self, f"_{attr_prefix}_canvas", cv)
        setattr(self, f"_{attr_prefix}_window", window_id)

        def on_mw(e):
            if getattr(e, "num", None) == 4:
                delta = -1
            elif getattr(e, "num", None) == 5:
                delta = 1
            else:
                delta = -1 if e.delta > 0 else 1
            cv.yview_scroll(delta, "units")
            return "break"

        def bind_mousewheel_tree(widget):
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                widget.bind(seq, on_mw)
            for child in widget.winfo_children():
                bind_mousewheel_tree(child)

        def refresh_scrollregion(_event=None):
            cv.configure(scrollregion=cv.bbox("all"))
            cv.itemconfigure(window_id, width=max(1, cv.winfo_width()))
            bind_mousewheel_tree(body)

        body.bind("<Configure>", refresh_scrollregion)
        cv.bind("<Configure>", refresh_scrollregion)
        bind_mousewheel_tree(cv)
        cv.after_idle(refresh_scrollregion)
        return page, body

    def _setting_row(self, parent, title, description, var, on_toggle, on_color="accent"):
        """一行设置: 标题 + 描述 + Switch 控件 (v0.6.1 Canvas 自绘).
        返回 (frame, var) 以便调用方后续读取 var 状态."""
        c = self.mw.c
        card = tk.Frame(parent, bg=c["card"], highlightthickness=1, highlightbackground=c["glass_border"])
        card.pack(fill=tk.X, pady=4)
        # 左侧: 标题 + 描述
        txt_frame = tk.Frame(card, bg=c["card"])
        txt_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=14, pady=10)
        tk.Label(txt_frame, text=title, font=("Microsoft YaHei UI", 10, "bold"),
                 fg=c["fg"], bg=c["card"], anchor=tk.W, justify=tk.LEFT).pack(anchor=tk.W)
        if description:
            tk.Label(txt_frame, text=description, font=("Microsoft YaHei UI", 8),
                     fg=c["fg2"], bg=c["card"], anchor=tk.W, justify=tk.LEFT,
                     wraplength=320).pack(anchor=tk.W, pady=(2, 0))
        # 右侧: Canvas 自绘 Switch (v0.6.1)
        sw = Switch(card, variable=var, command=on_toggle, on_color=c.get(on_color))
        sw.cv.pack(side=tk.RIGHT, padx=14, pady=10)
        return card

    def _general_tab(self, p):
        c = self.mw.c
        page, f = self._scrollable_tab(p, "general")
        # 启动 / 行为
        self._section_label(f, "启动", "登录后是否自动进入")
        self.auto_start_var = tk.BooleanVar(value=config.get("auto_start", True))
        self._setting_row(f, "开机时自动启动",
                          "登录 Windows 后自动启动, 静默进入托盘",
                          self.auto_start_var, self._auto_start_toggle)
        if not getattr(sys, "frozen", False):
            tk.Label(f, text="⚠ 当前为开发模式, 注册表项不会写入 (打包后才生效)",
                     font=("Microsoft YaHei UI", 8), fg=c["warn"], bg=c["bg"],
                     justify=tk.LEFT).pack(anchor=tk.W, pady=(4, 0))
        else:
            status = r"已注册到 HKCU\...\Run" if is_auto_start_enabled() else "未注册"
            tk.Label(f, text=f"当前状态: {status}",
                     font=("Microsoft YaHei UI", 8), fg=c["fg3"], bg=c["bg"],
                     justify=tk.LEFT).pack(anchor=tk.W, pady=(4, 0))

        # 隐私
        self._section_label(f, "录音隐私", "是否阻止其他 App 旁听")
        self.exclusive_var = tk.BooleanVar(value=config.get("exclusive_device", True))
        self._setting_row(f, "录音时独占设备",
                          "开启: 切走默认麦克风 + WASAPI 独占 (推荐)\n"
                          "关闭: 共享模式, 适合多人协作 / 会议场景",
                          self.exclusive_var, self._exclusive_toggle)

        # 桌面
        self._section_label(f, "桌面", "关闭主窗口后是否留在桌面上")
        self.bubble_var = tk.BooleanVar(value=config.get("floating_bubble", True))
        self._setting_row(f, "悬浮气泡",
                          "关闭主窗口后, 在桌面显示一个小气泡\n"
                          "气泡状态反映当前功能 (待命/录音中/已关闭)",
                          self.bubble_var, self._bubble_toggle)

        # 关于区块
        self._section_label(f, "关于", "")
        info_card = tk.Frame(f, bg=c["card"], highlightthickness=1, highlightbackground=c["glass_border"])
        info_card.pack(fill=tk.X, pady=4)
        tk.Label(info_card, text="言栖 v0.5.0 (pre-release)", font=("Microsoft YaHei UI", 10, "bold"),
                 fg=c["fg"], bg=c["card"]).pack(anchor=tk.W, padx=14, pady=(10, 0))
        tk.Label(info_card, text="本地离线识别 · sherpa-onnx + SenseVoice",
                 font=("Microsoft YaHei UI", 8), fg=c["fg2"], bg=c["card"]).pack(anchor=tk.W, padx=14, pady=(2, 0))
        link = tk.Label(info_card, text="github.com/Xinyang-S/STT-YanQi",
                        font=("Consolas", 8), fg=c["accent"], bg=c["card"], cursor="hand2")
        link.pack(anchor=tk.W, padx=14, pady=(2, 10))
        return page

    def _auto_start_toggle(self):
        enabled = self.auto_start_var.get()
        config["auto_start"] = enabled
        set_auto_start(enabled)
        save_config()
        log(f"用户切换开机启动: {enabled}")

    def _exclusive_toggle(self):
        enabled = self.exclusive_var.get()
        config["exclusive_device"] = enabled
        save_config()
        log(f"用户切换独占设备: {enabled}")

    def _bubble_toggle(self):
        enabled = self.bubble_var.get()
        config["floating_bubble"] = enabled
        save_config()
        log(f"用户切换悬浮气泡: {enabled}")
        # v0.7.0 会在此接入 FloatingBubble.show()/hide()
        if hasattr(self.mw, "bubble") and self.mw.bubble is not None:
            try:
                if enabled: self.mw.bubble.show()
                else: self.mw.bubble.hide()
            except Exception as e: log(f"气泡同步失败: {e!r}")

    def _audio_tab(self, p):
        c = self.mw.c
        page, f = self._scrollable_tab(p, "audio")
        self._section_label(f, "选择麦克风", "设置后立即生效")
        devs = AudioRecorder.list_devices()
        self.dv = tk.StringVar(); self.dm = {}
        for idx, name, is_def in devs:
            lb = f"{name}  {'(默认)' if is_def else ''}"
            self.dm[lb] = idx
            if idx == config.get("input_device_index"): self.dv.set(lb)
            elif is_def and config.get("input_device_index") is None: self.dv.set(lb)
        if devs:
            list_card = tk.Frame(f, bg=c["card"], highlightthickness=1,
                                 highlightbackground=c["glass_border"])
            list_card.pack(fill=tk.BOTH, expand=True, pady=4)
            for lb, idx in self.dm.items():
                row = tk.Frame(list_card, bg=c["card"])
                row.pack(fill=tk.X, padx=2, pady=2)
                rb = tk.Radiobutton(row, text="  " + lb, variable=self.dv, value=lb,
                                    fg=c["fg"], bg=c["card"], selectcolor=c["card"],
                                    activebackground=c["card"], activeforeground=c["fg"],
                                    font=("Microsoft YaHei UI", 9), anchor=tk.W,
                                    cursor="hand2", bd=0, highlightthickness=0,
                                    command=self._dev_save)
                rb.pack(fill=tk.X, padx=10, pady=6)
            hint_text = "录音时"
            if config.get("exclusive_device", True):
                hint_text += "自动尝试 WASAPI 独占 + 切换系统默认麦克风, 其他 App 听不到您的声音"
            else:
                hint_text += "使用共享模式, 其他 App 也能正常获取音频"
            tk.Label(f, text=hint_text, font=("Microsoft YaHei UI", 8),
                     fg=c["fg3"], bg=c["bg"], justify=tk.LEFT, wraplength=420).pack(anchor=tk.W, pady=(8, 0))
        else:
            tk.Label(f, text="未检测到麦克风", font=("Microsoft YaHei UI", 10),
                     fg=c["err"], bg=c["bg"]).pack(pady=20)
        return page

    def _dev_save(self):
        lb = self.dv.get()
        if lb in self.dm:
            config["input_device_index"] = self.dm[lb]
            save_config()


# ═══════════════════════════════════════════════════════════
#  系统托盘
# ═══════════════════════════════════════════════════════════
def tray_toggle(icon, item=None):
    state["enabled"] = not state["enabled"]
    log(f"托盘切换: {'启用' if state['enabled'] else '禁用'}")
    ui_queue.put(("toggled", state["enabled"]))
    (sound_toggle_on if state["enabled"] else sound_toggle_off)()
    if not state["enabled"] and state["recording"]: stop_recording()

def tray_show(icon, item=None): ui_queue.put(("show", None))

def tray_exit(icon, item=None):
    state["enabled"] = False; state["recording"] = False
    # 兜底恢复 MicGuard: 如果录音中点退出, finally 可能来不及跑
    orig = _mic_guard_state.get("orig_id")
    if orig:
        try:
            _policy_set_default(orig)
            _mic_guard_state["orig_id"] = None
            log("退出时已恢复默认麦克风")
        except Exception as e:
            log(f"退出时恢复麦克风失败: {e}")
    log("退出"); icon.stop()
    # 兜底退出: pystray.stop 是非阻塞的, 用 os._exit 确保所有线程终结
    os._exit(0)

def tray_menu(icon):
    s = "已启用" if state["enabled"] else "已禁用"
    return pystray.Menu(
        pystray.MenuItem(f"状态: {s} | {state['engine'] or '待命'}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("启用/禁用", tray_toggle, default=True),
        pystray.MenuItem("显示主窗口", tray_show),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", tray_exit),
    )


# ═══════════════════════════════════════════════════════════
#  端到端链路测试 (--test)
# ═══════════════════════════════════════════════════════════
def run_e2e_test():
    """全链路测试"""
    if sys.stdout.encoding != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    print("=" * 55)
    print("  言栖 v0.5.0 - 全链路测试")
    print("=" * 55)

    # 1. 引擎
    print("\n[1/4] 检查本地识别引擎...")
    hl = load_config()
    print(f"  sherpa-onnx:  {'[OK] 已安装' if _HAS_SHERPA else '[FAIL] 未安装'}")
    d, t, m = _resolve_model_dir()
    print(f"  SenseVoice:   {'[OK] 已就绪' if d else '[FAIL] 模型缺失'}")
    if d:
        print(f"    目录: {d}")
    if not hl:
        print("\n  [FAIL] 本地引擎不可用, 测试终止")
        print("    pip install sherpa-onnx")
        print(f"    下载并解压模型到: models/{SHERPA_SENSE_VOICE_MODEL}/")
        print(f"    {SHERPA_SENSE_VOICE_URL}")
        return 1

    # 2. 音频设备
    print("\n[2/4] 检查音频设备...")
    devs = AudioRecorder.list_devices()
    if devs:
        for idx, name, is_def in devs:
            print(f"  [{'[OK]' if is_def else ' '}] [{idx}] {name}")
    else:
        print("  [FAIL] 未检测到麦克风")
        return 1

    # 3. WASAPI 独占模式检测
    print("\n[3/4] 检测 WASAPI 独占模式支持...")
    if _find_wasapi():
        print("  [OK] WASAPI 可用, 录音时将尝试独占模式")
    else:
        print("  - WASAPI 不可用, 将使用共享模式")

    # 4. 录音 + 识别
    print("\n[4/4] 录音 + 识别测试")
    print("  即将录音 3 秒, 请用中文说一句话...")
    for i in range(3, 0, -1):
        print(f"  {i}...")
        time.sleep(1)

    rec = AudioRecorder()
    rec.start()
    print(f"  ● 录音中 ({rec.mode}模式)... 请说话")
    time.sleep(3)
    frames = rec.stop()
    rec.close()
    print("  ● 录音结束")

    if not frames or len(frames) < 5:
        print("  [FAIL] 录音数据不足")
        return 1

    path = str(CONFIG_DIR / "test_audio.wav")
    AudioRecorder().save(frames, path)
    print(f"  已保存: {path} ({len(frames)} 帧)")

    print("  识别中...")
    asr = ASRManager()
    print(f"  可用引擎: {asr.count()} 个")

    try:
        txt, eng = asr.transcribe(path)
        print(f"  [OK] [{eng}] 识别结果: {txt}")
    except Exception as e:
        print(f"  [FAIL] 识别失败: {e}")
        traceback.print_exc()
        return 1

    print("\n" + "=" * 55)
    print("  测试通过!")
    print(f"  识别结果: {txt}")
    print(f"  使用引擎: {eng}")
    print(f"  音频模式: {rec.mode}")
    print("=" * 55)
    return 0


# ═══════════════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════════════
def main():
    # --test 模式
    if "--test" in sys.argv:
        sys.exit(run_e2e_test())

    # --recognize <file> 模式: 仅识别指定 wav 文件 (调试用)
    if "--recognize" in sys.argv:
        try:
            idx = sys.argv.index("--recognize")
            audio_path = sys.argv[idx + 1]
        except (IndexError, ValueError):
            print("用法: VoiceInput.exe --recognize <wav 文件>")
            return 1
        if sys.stdout.encoding != 'utf-8':
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        print(f"识别文件: {audio_path}")
        load_config()
        asr = ASRManager()
        try:
            txt, eng = asr.transcribe(audio_path)
            print(f"[{eng}] {txt}")
            return 0
        except Exception as e:
            print(f"[FAIL] {e}")
            traceback.print_exc()
            return 1

    minimized = "--minimized" in sys.argv

    log(f"言栖 v0.5.0 启动 (minimized={minimized})")
    hl = load_config()
    if not hl:
        messagebox.showerror("本地引擎不可用",
            "未检测到可用的本地识别引擎\n\n"
            "请确认以下步骤:\n"
            "  1) pip install sherpa-onnx\n"
            f"  2) 下载模型: sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17\n"
            f"     {SHERPA_SENSE_VOICE_URL}\n"
            f"  3) 解压到 models/{SHERPA_SENSE_VOICE_MODEL}/\n"
            f"  4) 或在设置中指定 model_dir\n\n"
            f"配置文件: {CONFIG_FILE}\n\n"
            "也可以先运行: VoiceInput.exe --test 查看详细诊断")
        sys.exit(1)
    log(f"本地{'√' if hl else '×'}")

    # 首次启动: 若 auto_start=True 且未注册, 自动写入 HKCU Run
    if config.get("auto_start", True) and not is_auto_start_enabled():
        set_auto_start(True)

    kb = keyboard.Listener(on_press=on_press, on_release=on_release); kb.daemon = True; kb.start()
    log("键盘监听已启动 (Right Ctrl 录音, Ctrl+Shift+F9 开关)")

    icon = pystray.Icon("voice_input", icon=ic_idle, title="言栖"); icon.menu = tray_menu(icon)
    win = MainWindow(icon, start_minimized=minimized)

    stopped = threading.Event()
    def _tray(): icon.run(); stopped.set()
    threading.Thread(target=_tray, daemon=True).start()

    # 开机启动时不弹提示, 手动启动才提示快捷键
    if not minimized:
        threading.Timer(1.5, lambda: icon.notify(
            "按住 Right Ctrl 录音 → 松开识别\n"
            "Ctrl+Shift+F9 开关\n"
            "设置中可调整: 开机启动 / 独占设备 / 麦克风 / 识别语言",
            "言栖 v0.5.0"
        )).start()

    # X 按钮: 已在 MainWindow.__init__ 中通过 protocol 绑定到 win._on_close
    # (隐藏主窗口 + 显示气泡)
    win.root.mainloop()
    stopped.set(); kb.stop(); state["enabled"] = False; log("程序退出")


if __name__ == "__main__":
    main()
