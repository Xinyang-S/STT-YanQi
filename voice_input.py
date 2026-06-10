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
def _gen_tone(freqs_durs, sample_rate=22050):
    """生成带泛音和包络的 WAV 音频数据字节串"""
    samples = np.array([], dtype=np.float32)
    for freq, dur in freqs_durs:
        n = int(sample_rate * dur)
        t = np.linspace(0, dur, n, endpoint=False)
        # 基频 + 2次泛音 + 3次泛音 (模拟 Discord 的清脆感)
        tone = (np.sin(2 * np.pi * freq * t)
                + 0.4 * np.sin(2 * np.pi * freq * 2 * t)
                + 0.15 * np.sin(2 * np.pi * freq * 3 * t))
        # 指数衰减包络
        env = np.exp(-t * (3.0 / dur if dur > 0 else 30))
        tone *= env * 0.7
        samples = np.concatenate([samples, tone])
    # 归一化到 int16
    peak = np.max(np.abs(samples)) or 1
    samples = (samples / peak * 28000).astype(np.int16)
    # 拼 WAV 头
    data = samples.tobytes()
    hdr = struct.pack('<4sI4s4sIHHIIHH4sI',
        b'RIFF', 36 + len(data), b'WAVE', b'fmt ', 16,
        1, 1, sample_rate, sample_rate * 2, 2, 16, b'data', len(data))
    return hdr + data

# Discord 风格音效预设
_WAV_START     = _gen_tone([(880, 0.06), (1320, 0.08)])   # 上升: 类似 unmute (双音更醒目)
_WAV_DONE      = _gen_tone([(1568, 0.12)])                 # 叮: 类似 DM 通知
_WAV_TOGGLE_ON  = _gen_tone([(1047, 0.08), (1318, 0.06)])  # 双音: 启用 = 上行
_WAV_TOGGLE_OFF = _gen_tone([(1318, 0.06), (784, 0.08)])   # 双音: 禁用 = 下行
_WAV_ERROR     = _gen_tone([(262, 0.15)])                 # 低沉
_WAV_FALLBACK  = _gen_tone([(660, 0.04), (660, 0.04)])    # 双声

try:
    import winsound
    # 关键约束 (实测 Python 3.13):
    #   1. winsound.PlaySound 不支持 SND_MEMORY | SND_ASYNC (运行时抛 RuntimeError)
    #   2. v4.3 的 SND_ASYNC + 临时文件方案有竞态: PlaySound 立即返回, 但 Windows
    #      音频子系统可能延迟数十~数百毫秒才真正开始读文件; 若主线程在 200-300ms
    #      内把 temp 文件 unlink 掉, 整段声音被截断, 表现就是 "明明调了 _play
    #      却听不到" — 启动提示音特别容易丢 (按下到下一个事件间隔短).
    #
    # 修复 (v0.5.0): 用同步模式 (不带 SND_ASYNC) 在独立线程里播, 线程自然阻塞到
    # 播完才返回, 不依赖任何临时文件, 也没有"窗口期删除"的竞态. 主线程零阻塞.
    # _SOUND_QUEUE 串行化所有声音请求, 避免 PlaySound 内部相互打断
    # (Windows PlaySound 自身就是"单音"语义, 后调用的会顶掉前一个).
    _SOUND_QUEUE = queue_mod.Queue()
    def _sound_worker():
        while True:
            name = _SOUND_QUEUE.get()
            if name is None: return
            try:
                path = _SOUND_FILE_PATHS.get(name)
                if path and os.path.isfile(path):
                    # 不带 SND_ASYNC = 同步阻塞, 播完才返回; SND_NODEFAULT: 找不到时不放系统默认音
                    # 持久文件确保 Windows 读文件时不会撞上 "文件已删除" 截断
                    winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_NODEFAULT)
            except Exception as e:
                log(f"音效[{name}]播放失败: {e!r}")
    _SOUND_FILE_PATHS = {}
    _SOUND_READY = False

    def _init_sounds():
        """首次调用 _play 时初始化: 把所有 WAV 写入 ~/.voice_input/sounds/ 持久文件,
        之后 PlaySound 直接读盘, 不再有删除竞态."""
        global _SOUND_READY
        if _SOUND_READY: return
        try:
            sound_dir = CONFIG_DIR / "sounds"
            sound_dir.mkdir(parents=True, exist_ok=True)
            for name, wav in [("start", _WAV_START), ("done", _WAV_DONE),
                              ("toggle_on", _WAV_TOGGLE_ON), ("toggle_off", _WAV_TOGGLE_OFF),
                              ("error", _WAV_ERROR), ("fallback", _WAV_FALLBACK)]:
                path = sound_dir / f"{name}.wav"
                path.write_bytes(wav)
                _SOUND_FILE_PATHS[name] = str(path)
            t = threading.Thread(target=_sound_worker, daemon=True)
            t.start()
            _SOUND_READY = True
        except Exception as e:
            log(f"音效初始化失败: {e!r}")

    def _play(name):
        """排队播放一个音效. 调用方零阻塞, 实际播放由后台串行 worker 处理.
        name: "start" / "done" / "toggle_on" / "toggle_off" / "error" / "fallback"."""
        if not _SOUND_READY: _init_sounds()
        try: _SOUND_QUEUE.put_nowait(name)
        except Exception: pass

    def _play_sync(name):
        """保留 API 兼容: 同步播放某个声音 (用 temp 文件, 当前未使用)."""
        _play(name)
except ImportError:
    def _play(name):  return
    def _play_sync(name):  return

# v0.5.0: 音效统一为命名 + 后台串行 worker 模式. 修复按下快捷键时无提示音:
# 之前 sound_start 用 SND_ASYNC + 200ms 临时文件, 在 Windows 实际开始读文件
# 之前就被 unlink → 启动音被截断. 现在用持久文件 + 串行 worker, 100% 可闻.
def sound_start():       _play("start")
def sound_done():        _play("done")
def sound_toggle_on():   _play("toggle_on")
def sound_toggle_off():  _play("toggle_off")
def sound_error():       _play("error")
def sound_fallback():    _play("fallback")


# ═══════════════════════════════════════════════════════════
#  WASAPI 独占模式 (录音时阻止其他 app 获取麦克风)
# ═══════════════════════════════════════════════════════════

# PortAudio WASAPI 结构有两种版本:
# V1 (5字段, 20字节): size + hostApiType + version + flags + channelMask
# V2 (7字段, 28字节): 以上5字段 + streamCategory + streamOption
# 我们两种都尝试，哪个能匹配就用哪个
PA_WASAPI_FLAG_EXCLUSIVE = 0x1

class _WasapiInfoV1(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_ulong),
        ("hostApiType", ctypes.c_long),
        ("version", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("channelMask", ctypes.c_ulong),
    ]

class _WasapiInfoV2(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_ulong),
        ("hostApiType", ctypes.c_long),
        ("version", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("channelMask", ctypes.c_ulong),
        ("streamCategory", ctypes.c_ulong),
        ("streamOption", ctypes.c_ulong),
    ]

_wasapi_host_type = None

def _find_wasapi():
    global _wasapi_host_type
    if _wasapi_host_type is not None:
        return _wasapi_host_type
    p = pyaudio.PyAudio()
    for i in range(p.get_host_api_count()):
        info = p.get_host_api_info_by_index(i)
        if 'wasapi' in info['name'].lower():
            _wasapi_host_type = info['type']
            break
    p.terminate()
    return _wasapi_host_type

def _try_wasapi_with_struct(pa_instance, struct_class, callback, ht, sample_rate, channels, chunk, device_index):
    """用给定的结构体尝试打开 WASAPI 独占流"""
    info = struct_class()
    info.size = ctypes.sizeof(struct_class)
    info.hostApiType = ht
    info.version = 1
    info.flags = PA_WASAPI_FLAG_EXCLUSIVE
    info.channelMask = 0
    if struct_class is _WasapiInfoV2:
        info.streamCategory = 0
        info.streamOption = 0

    kw = dict(format=pyaudio.paInt16, channels=channels, rate=sample_rate,
              input=True, frames_per_buffer=chunk, stream_callback=callback,
              input_host_api_specific_stream_info=ctypes.pointer(info))
    if device_index is not None:
        kw["input_device_index"] = device_index

    stream = pa_instance.open(**kw)
    return stream

def try_open_exclusive_stream(pa_instance, callback, sample_rate, channels, chunk, device_index):
    """尝试 WASAPI 独占模式。失败返回 None"""
    ht = _find_wasapi()
    if ht is None:
        return None, "WASAPI 不可用"

    # 优先尝试 V2 结构 (新版 PortAudio), 再试试 V1
    for struct_class in (_WasapiInfoV2, _WasapiInfoV1):
        try:
            stream = _try_wasapi_with_struct(
                pa_instance, struct_class, callback, ht, sample_rate, channels, chunk, device_index)
            # 验证独占: 尝试打开第二个流, 应该失败
            try:
                p2 = pyaudio.PyAudio()
                s2 = p2.open(format=pyaudio.paInt16, channels=channels, rate=sample_rate,
                             input=True, frames_per_buffer=chunk)
                s2.close(); p2.terminate()
                # 第二个流也成功了 = 独占未生效, 关闭第一个重试
                stream.close()
                log(f"WASAPI struct={struct_class.__name__} 独占未生效(第二流成功)")
                continue
            except Exception:
                p2.terminate()
                log(f"WASAPI 独占确认: {struct_class.__name__} size={ctypes.sizeof(struct_class)}")
                return stream, "WASAPI 独占"
        except Exception as e:
            log(f"WASAPI {struct_class.__name__} 失败: {e}")
            continue

    return None, "WASAPI 尝试失败"


# ═══════════════════════════════════════════════════════════
#  录音时切换默认麦克风 — 纯 ctypes COM, 不依赖 comtypes
#  原理: CoCreateInstance + vtable 直接调用
#  IPolicyConfig::SetDefaultEndpoint (SoundSwitch/EarTrumpet 同款方案)
# ═══════════════════════════════════════════════════════════
from ctypes import (c_int, c_uint, c_ulong, c_ushort, c_void_p, c_wchar_p, wintypes,
                    POINTER, pointer, byref, cast, Structure, sizeof,
                    WINFUNCTYPE)

from comtypes import GUID

ole32 = ctypes.windll.ole32
ole32.CoCreateInstance.argtypes = [POINTER(GUID), c_void_p, c_ulong, POINTER(GUID), POINTER(c_void_p)]
ole32.CoCreateInstance.restype = ctypes.c_long
ole32.CoTaskMemFree.argtypes = [c_void_p]  # 64-bit 地址, 默认 c_int 会 overflow
ole32.CoTaskMemFree.restype = None
ole32.CoInitialize.argtypes = [c_void_p]
ole32.CoInitialize.restype = ctypes.c_long
ole32.CoUninitialize.argtypes = []
ole32.CoUninitialize.restype = None

# COM GUID helpers
def _guid(s):
    g = GUID()
    ole32.CLSIDFromString(s, byref(g))
    return g

CLSID_MMDeviceEnumerator    = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
CLSID_PolicyConfig          = "{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}"
IID_IMMDeviceEnumerator     = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IMMDevice               = "{D666063F-1587-4E43-81F1-B948E807363F}"
IID_IPropertyStore          = "{886d8eeb-8cf2-4446-8d02-cdba1dbdcf99}"
# IPolicyConfig 的三个 IID 变体 (依次尝试)
IID_PolicyConfig10  = "{824A9E1A-FE9E-47A3-AD79-309400D00B37}"  # Win10
IID_PolicyConfig    = "{F8679F50-850A-41CF-9C72-430F290290C8}"  # Win10+
IID_PolicyConfigV   = "{568B9108-44BF-40B4-9006-86AFE1B5E620}"  # Win8-

CLSCTX_INPROC = 1
STGM_READ = 0
eCapture, eConsole = 1, 0
DEVICE_STATE_ACTIVE = 1
# PKEY_Device_FriendlyName = {a45c254e-df1c-4efd-8020-67d146a850e0}, pid=2
# (注意末尾是 0e0 不是 0a0, 这是 Windows 官方 property key 规范)
_PKEY_FMTID_FRIENDLYNAME = "{a45c254e-df1c-4efd-8020-67d146a850e0}"
_PKEY_PID_FRIENDLYNAME = 2
VT_LPWSTR = 31

def _com_vtbl_call(this, idx, restype, *argtypes):
    """通过 vtable 直接调用 COM 方法"""
    vtbl = cast(this, POINTER(POINTER(c_void_p)))
    func_ptr = vtbl[0][idx]
    proto = WINFUNCTYPE(restype, c_void_p, *argtypes)
    return proto(func_ptr)

def _com_query_interface(this, iid_str):
    """COM QueryInterface 返回指定接口指针"""
    ppv = c_void_p()
    riid = _guid(iid_str)
    _com_vtbl_call(this, 0, ctypes.c_long, POINTER(GUID), POINTER(c_void_p))(
        this, byref(riid), byref(ppv))
    return ppv.value if ppv.value else None

def _com_get_device_id(mmdev_ptr):
    """IMMDevice::GetId"""
    pp = c_void_p()
    _com_vtbl_call(mmdev_ptr, 5, ctypes.c_long, POINTER(c_void_p))(
        mmdev_ptr, byref(pp))
    if not pp.value: return None
    s = cast(pp.value, c_wchar_p).value
    ole32.CoTaskMemFree(pp)  # 传 c_void_p 而非 int, 避免 64bit 溢出
    return s

def _com_release(ptr):
    """IUnknown::Release"""
    if ptr:
        _com_vtbl_call(ptr, 2, c_ulong)(ptr)

class PROPERTYKEY(ctypes.Structure):
    """Windows PROPERTYKEY = GUID fmtid (16) + DWORD pid (4) = 20 bytes"""
    _fields_ = [("fmtid", GUID), ("pid", c_ulong)]

def _com_get_friendly_name(mmdev_ptr):
    """IMMDevice::OpenPropertyStore + IPropertyStore::GetValue(PKEY_Device_FriendlyName)
    返回 (Windows 设备 friendly name 字符串). 失败返回 None.

    注意: 这是修复 pyaudio 设备名乱码的关键 — pyaudio 0.2.14 在 Windows 上对某些
    设备名字符串解码错误 (返回像 "鍐呴儴 AUX 鎻掑骇" 这种 GBK→UTF-8 roundtrip 错字).
    用 IMMDevice + IPropertyStore 直接拿 UTF-16 friendly name 完全正确.
    """
    try:
        # OpenPropertyStore (vtable=4, STGM_READ=0)
        ps = c_void_p()
        hr = _com_vtbl_call(mmdev_ptr, 4, ctypes.c_long, c_ulong, POINTER(c_void_p))(
            mmdev_ptr, c_ulong(STGM_READ), byref(ps))
        if hr < 0 or not ps.value: return None
        try:
            # 构造 PROPERTYKEY
            pkey = PROPERTYKEY()
            pkey.fmtid = _guid(_PKEY_FMTID_FRIENDLYNAME)
            pkey.pid = _PKEY_PID_FRIENDLYNAME
            # GetValue 写入 PROPVARIANT (32 字节 x64, 16 字节 x86; 32 足够)
            pv = (ctypes.c_ubyte * 32)()
            hr2 = _com_vtbl_call(ps, 5, ctypes.c_long, POINTER(PROPERTYKEY), c_void_p)(
                ps, byref(pkey), ctypes.addressof(pv))
            if hr2 < 0: return None
            vt = c_ushort.from_address(ctypes.addressof(pv)).value
            if vt != VT_LPWSTR: return None
            # pwszVal 在 +8 偏移 (PROPVARIANT: vt(2) + wReserved(6) + union...)
            pptr = c_void_p.from_address(ctypes.addressof(pv) + 8).value
            if not pptr: return None
            name = cast(pptr, c_wchar_p).value
            # 复制一份 (CoTaskMemFree 会释放原 buffer)
            name = str(name) if name else None
            ole32.CoTaskMemFree(pptr)
            return name
        finally:
            _com_release(ps)
    except Exception as e:
        log(f"_com_get_friendly_name: {e}")
        return None


def _enum_capture_devices_com():
    """用 IMMDeviceEnumerator 枚举所有激活的 capture (输入) 设备, 返回:
    [(device_id_str, friendly_name, is_default), ...]

    替代 pyaudio 枚举的原因: pyaudio 0.2.14 在 Windows 上对部分设备名解码出错
    (例如 "鍐呴儴 AUX 鎻掑骇" 这种 GBK→UTF-8 roundtrip 错字, 应该是 "内部 AUX 插座").
    """
    devices = []
    default_id = None
    # COM 必须在 STA 或 MTA 线程初始化. MicGuard 调用方可能已 init, 这里也 init
    # 一次保证独立工作.
    co_init = ole32.CoInitialize(None)
    try:
        # CoCreate MMDeviceEnumerator
        mmde = c_void_p()
        hr = ole32.CoCreateInstance(
            byref(_guid(CLSID_MMDeviceEnumerator)), c_void_p(), c_ulong(CLSCTX_INPROC),
            byref(_guid(IID_IMMDeviceEnumerator)), byref(mmde))
        if hr < 0 or not mmde.value:
            log(f"_enum_capture_devices_com: CoCreateInstance 失败 0x{hr:X}")
            return devices
        try:
            # 拿默认 capture 设备的 id (用来标 is_default)
            default_dev = c_void_p()
            hr2 = _com_vtbl_call(mmde, 4, ctypes.c_long, c_int, c_int, POINTER(c_void_p))(
                mmde, eCapture, eConsole, byref(default_dev))
            if hr2 >= 0 and default_dev.value:
                default_id = _com_get_device_id(default_dev.value)
                _com_release(default_dev)

            # EnumAudioEndpoints(eCapture, DEVICE_STATE_ACTIVE)
            col = c_void_p()
            hr3 = _com_vtbl_call(mmde, 3, ctypes.c_long, c_int, c_uint, POINTER(c_void_p))(
                mmde, eCapture, DEVICE_STATE_ACTIVE, byref(col))
            if hr3 < 0 or not col.value:
                log(f"_enum_capture_devices_com: EnumAudioEndpoints 失败 0x{hr3:X}")
                return devices
            try:
                cnt = c_uint()
                _com_vtbl_call(col, 3, ctypes.c_long, POINTER(c_uint))(col, byref(cnt))
                for i in range(cnt.value):
                    dev = c_void_p()
                    _com_vtbl_call(col, 4, ctypes.c_long, c_uint, POINTER(c_void_p))(
                        col, c_uint(i), byref(dev))
                    if not dev.value: continue
                    try:
                        did = _com_get_device_id(dev.value)
                        name = _com_get_friendly_name(dev.value)
                        if did and name:
                            devices.append((did, name, did == default_id))
                    finally:
                        _com_release(dev)
            finally:
                _com_release(col)
        finally:
            _com_release(mmde)
        if co_init == 0:  # S_OK 表示这次是首次 init, 平衡一下; RPC_E_CHANGED_MODE 表明已 init
            ole32.CoUninitialize()
    except Exception as e:
        log(f"_enum_capture_devices_com 异常: {e}\n{traceback.format_exc()}")
    return devices

# MicGuard 全局状态: 记录"当前活跃的原始设备 ID", 防止 tray_exit 等硬退出路径漏恢复
_mic_guard_state = {"orig_id": None}

def _policy_set_default(device_id):
    """模块级: IPolicyConfig::SetDefaultEndpoint — 把指定设备设为默认 (按 role 切三类)"""
    for iid_name, iid in [("10", IID_PolicyConfig10), ("def", IID_PolicyConfig), ("v", IID_PolicyConfigV)]:
        pc = c_void_p()
        hr = ole32.CoCreateInstance(
            byref(_guid(CLSID_PolicyConfig)), c_void_p(), c_ulong(CLSCTX_INPROC),
            byref(_guid(iid)), byref(pc))
        if hr >= 0 and pc.value:
            call = _com_vtbl_call(pc, 13, ctypes.c_long, c_wchar_p, c_int)
            for role, rname in [(0, "mult"), (1, "multi"), (2, "comm")]:
                hr2 = call(pc, device_id, role)
                log(f"PolicySetDefault [{iid_name}:{rname}] hr=0x{hr2:X}")
            return
    raise Exception("IPolicyConfig unavailable")


class MicGuard:
    """上下文管理器: 录音时把系统默认麦克风切到回退设备, 阻止其他 app 拿到我们说的话

    用法:
        g = MicGuard()
        g.__enter__()         # 切换 (无回退设备时抛异常)
        ... 录音 ...
        g.__exit__(...)       # 恢复

    副作用: 切换瞬间其他从默认设备取数据的 app 会断流 (可能闪退/重连),
            退出 __exit__ 后立即恢复。
    """

    def __init__(self):
        self._orig_id = None
        self._fallback_id = None
        self._fallback_was_inactive = False  # 修复: 原代码漏初始化, _restore 引用会 AttributeError

    def __enter__(self):
        self._switch()
        _mic_guard_state["orig_id"] = self._orig_id  # 供 tray_exit 兜底恢复
        return self

    def __exit__(self, *args):
        if self._orig_id:
            try:
                _policy_set_default(self._orig_id)
                log("MicGuard: default restored")
            except Exception as e:
                log(f"MicGuard restore: {e}")
        _mic_guard_state["orig_id"] = None

    def _switch(self):
        mmde = c_void_p()
        hr = ole32.CoCreateInstance(
            byref(_guid(CLSID_MMDeviceEnumerator)), c_void_p(), c_ulong(CLSCTX_INPROC),
            byref(_guid(IID_IMMDeviceEnumerator)), byref(mmde))
        if hr < 0: raise Exception(f"CoCreateInstance MMDE failed 0x{hr:X}")

        cur = c_void_p()
        _com_vtbl_call(mmde, 4, ctypes.c_long, c_int, c_int, POINTER(c_void_p))(
            mmde, eCapture, eConsole, byref(cur))
        if not cur.value: raise Exception("no default capture device")
        self._orig_id = _com_get_device_id(cur.value)
        if not self._orig_id: raise Exception("can't get device id")
        log(f"MicGuard: orig default = {self._orig_id[:40]}...")

        # 找回退设备: 第一个活跃且非默认的
        fallback = None
        col = c_void_p()
        _com_vtbl_call(mmde, 3, ctypes.c_long, c_int, c_uint, POINTER(c_void_p))(
            mmde, eCapture, DEVICE_STATE_ACTIVE, byref(col))
        if col.value:
            cnt = c_uint()
            _com_vtbl_call(col, 3, ctypes.c_long, POINTER(c_uint))(col, byref(cnt))
            for i in range(cnt.value):
                dev = c_void_p()
                _com_vtbl_call(col, 4, ctypes.c_long, c_uint, POINTER(c_void_p))(
                    col, c_uint(i), byref(dev))
                if not dev.value: continue
                did = _com_get_device_id(dev.value)
                if did and did != self._orig_id:
                    fallback = did
                    log(f"MicGuard: fallback = {did[:40]}...")
                    break
        if not fallback:
            raise Exception("no fallback device (需要至少 2 个录音设备才能独占)")

        self._fallback_id = fallback
        _policy_set_default(fallback)
        log("MicGuard: default -> fallback (其他 app 已切走)")
        time.sleep(0.15)  # 等其他 app 释放对原设备的访问

    def _set_visibility(self, device_id, visible):
        """IPolicyConfig::SetEndpointVisibility — 启用/禁用设备 (vtable=14)"""
        for iid_name, iid in [("10", IID_PolicyConfig10), ("def", IID_PolicyConfig), ("v", IID_PolicyConfigV)]:
            pc = c_void_p()
            hr = ole32.CoCreateInstance(
                byref(_guid(CLSID_PolicyConfig)), c_void_p(), c_ulong(CLSCTX_INPROC),
                byref(_guid(iid)), byref(pc))
            if hr >= 0 and pc.value:
                hr2 = _com_vtbl_call(pc, 14, ctypes.c_long, c_wchar_p, c_int)(
                    pc, device_id, visible)
                log(f"MicGuard visibility [{iid_name}] visible={visible} hr=0x{hr2:X}")
                if hr2 == 0: return True
        return False

    # 注: SetDefault 逻辑已上提到模块级 _policy_set_default, 供 tray_exit 兜底调用


# ═══════════════════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════════════════
CONFIG_DIR = Path.home() / ".voice_input"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = CONFIG_DIR / "voice_input.log"

DEFAULT_CONFIG = {
    "sample_rate": 16000,
    "channels": 1,
    "chunk_size": 1024,
    "input_device_index": None,
    "auto_start": True,
    "exclusive_device": True,  # 录音时独占音频设备 (WASAPI 独占 + 切默认麦克风)
    "language": "auto",         # auto / zh / en / ja / ko / yue
    "model_dir": "",            # 模型目录; 留空时按优先级自动搜索
    "floating_bubble": False,  # 关闭主窗口后是否显示悬浮气泡 (默认关闭)
    "bubble_x": None,           # 气泡 X 坐标 (持久化); None = 屏幕右侧默认
    "bubble_y": None,           # 气泡 Y 坐标 (持久化)
}

# sherpa-onnx SenseVoice 多语种模型 (int8) — 离线本地引擎, 唯一识别后端
# 下载: https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
SHERPA_SENSE_VOICE_MODEL = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
SHERPA_SENSE_VOICE_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2"
)


def _resolve_model_dir():
    """解析本地模型目录. 优先级:
      1) config.model_dir (用户指定)
      2) sys._MEIPASS/models/SHERPA_SENSE_VOICE_MODEL (PyInstaller --onefile 解压根)
      3) <exe 所在目录>/models/SHERPA_SENSE_VOICE_MODEL (frozen --onedir 模式)
      4) ./models/SHERPA_SENSE_VOICE_MODEL (开发模式)
      5) ./models (开发模式, 直接用 models 目录)
    返回 (model_dir, tokens, model_file). 任一缺失则 None.
    """
    candidates = []
    cfg_dir = config.get("model_dir", "") if isinstance(config.get("model_dir"), str) else ""
    if cfg_dir:
        candidates.append(cfg_dir)
    # PyInstaller --onefile: 资源解压到 sys._MEIPASS
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "models", SHERPA_SENSE_VOICE_MODEL))
        candidates.append(os.path.join(meipass, "models"))
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        candidates.append(os.path.join(exe_dir, "models", SHERPA_SENSE_VOICE_MODEL))
        candidates.append(os.path.join(exe_dir, "models"))
    candidates.append(os.path.join(os.getcwd(), "models", SHERPA_SENSE_VOICE_MODEL))
    candidates.append(os.path.join(os.getcwd(), "models"))

    for d in candidates:
        tokens = os.path.join(d, "tokens.txt")
        model  = os.path.join(d, "model.int8.onnx")
        if os.path.isfile(tokens) and os.path.isfile(model):
            return d, tokens, model
    return None, None, None

APP_NAME = "VoiceInput"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def is_auto_start_enabled() -> bool:
    """检查 HKCU Run 键中是否存在本应用的启动项"""
    if not getattr(sys, "frozen", False):
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except Exception as e:
        log(f"检查开机启动失败: {e}")
        return False


def set_auto_start(enabled: bool):
    """写入/删除 HKCU Run 启动项. 仅 frozen 模式生效 (开发态写死 python.exe 路径会反复登记)"""
    if not getattr(sys, "frozen", False):
        log("[开发模式] 跳过开机启动注册表写入 (打包后才生效)")
        return
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                # --minimized: 开机启动时直接进托盘, 不弹主窗口
                cmd = f'"{sys.executable}" --minimized'
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
                log(f"开机启动已启用: {cmd}")
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                    log("开机启动已禁用")
                except FileNotFoundError:
                    pass
    except Exception as e:
        log(f"设置开机启动失败: {e}")

config = {}
state = {"enabled": True, "recording": False, "engine": "none",
         "last_text": "", "last_error": "", "audio_mode": "共享",
         "mic_guarded": False, "exclusive": True}
ui_queue = queue_mod.Queue()


def log(msg: str):
    try:
        ts = time.strftime("%H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def load_config():
    global config
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            try:
                saved = json.load(f)
            except json.JSONDecodeError:
                saved = {}
        config = {**DEFAULT_CONFIG, **saved}
        # 清理旧版字段 (baidu / iflytek / local_asr)
        for k in ("baidu", "iflytek", "local_asr"):
            config.pop(k, None)
        # 补全新字段
        for k, v in DEFAULT_CONFIG.items():
            config.setdefault(k, v)
    else:
        config = DEFAULT_CONFIG.copy()
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

    # 本地引擎: sherpa-onnx 包 + 模型文件全部就绪才算"可用"
    d, t, m = _resolve_model_dir()
    has_local = _HAS_SHERPA and d is not None
    if has_local:
        log(f"本地引擎就绪: {d}")
    else:
        reasons = []
        if not _HAS_SHERPA: reasons.append("sherpa-onnx 未安装")
        if d is None:       reasons.append(f"模型文件缺失 (需要 {SHERPA_SENSE_VOICE_MODEL})")
        log(f"本地引擎不可用: {'; '.join(reasons)}")
    return has_local


def save_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════
#  ASR 引擎: sherpa-onnx + SenseVoice 多语种
#  - 单文件, 离线, CPU 友好
#  - 自动检测 zh / en / ja / ko / yue
#  - 模型: sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17 (~120MB)
# ═══════════════════════════════════════════════════════════
class LocalASR:
    """sherpa-onnx SenseVoice 离线引擎.

    调优:
      - 模型 ~120MB (int8), 加载 1-2s, 识别实时 (CPU)
      - 支持语言: auto / zh / en / ja / ko / yue
      - auto 模式由 SenseVoice 自动检测 (≤100ms)
    """
    name = "本地"
    _shared = None        # 类级单例, 避免每次录音都重新加载
    _shared_lock = threading.Lock()
    _shared_err = None

    def __init__(self, model_dir, tokens, model_file, language="auto"):
        if not _HAS_SHERPA:
            raise ImportError("sherpa-onnx 未安装")
        self.model_dir = model_dir
        self.tokens = tokens
        self.model_file = model_file
        self.language = language or "auto"
        self._recognizer = None

    def _ensure_loaded(self):
        if self._recognizer is not None:
            return True
        with LocalASR._shared_lock:
            if LocalASR._shared is not None and LocalASR._shared.get("dir") == self.model_dir:
                self._recognizer = LocalASR._shared["obj"]
                return True
            try:
                log(f"本地引擎加载模型: {self.model_dir}")
                # language 在构造时设置 (而非 stream.set_option) —
                # 后者在 PyInstaller 打包下会抛 "invalid unordered_map<K, T> key"
                lang = self.language if self.language in ("auto", "zh", "en", "ja", "ko", "yue") else "auto"
                rec = OfflineRecognizer.from_sense_voice(
                    model=self.model_file,
                    tokens=self.tokens,
                    num_threads=max(1, os.cpu_count() or 4),
                    use_itn=True,
                    debug=False,
                    provider="cpu",
                    language=lang,
                )
                LocalASR._shared = {"dir": self.model_dir, "obj": rec}
                self._recognizer = rec
                log(f"本地引擎就绪 (language={lang})")
                return True
            except Exception as e:
                LocalASR._shared_err = e
                log(f"本地引擎模型加载失败: {e!r}")
                return False

    def transcribe(self, audio_path):
        if not self._ensure_loaded():
            raise Exception(f"本地引擎不可用: {LocalASR._shared_err}")
        # 读 16kHz PCM float32; 没有 soundfile 时用 wave
        try:
            import soundfile as sf
            samples, sr = sf.read(audio_path, dtype="float32")
            if sr != 16000:
                log(f"警告: 音频采样率 {sr} != 16000, 识别可能不准")
        except ImportError:
            import wave
            with wave.open(audio_path, "rb") as wf:
                sr = wf.getframerate()
                raw = wf.readframes(wf.getnframes())
                ch = wf.getnchannels()
                sw = wf.getsampwidth()
            if sw != 2 or ch != 1:
                raise Exception(f"音频格式不支持 (sr={sr}, ch={ch}, sw={sw})")
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if len(samples) == 0:
            return ""

        s = self._recognizer.create_stream()
        s.accept_waveform(16000, samples)
        # 离线模式无需 input_finished, decode_stream 直接消费波形
        self._recognizer.decode_stream(s)
        txt = (s.result.text or "").strip()
        # 剥掉 SenseVoice 的标签: <|zh|><|NEUTRAL|><|Speech|><|withitn|>
        txt = re.sub(r"<\|[^|]+\|>", "", txt).strip()
        return txt


class ASRManager:
    """仅含一个本地引擎; 保留多 engine 接口以便未来扩展."""
    def __init__(self):
        self.engines = []
        d, t, m = _resolve_model_dir()
        if _HAS_SHERPA and d is not None:
            try:
                lang = config.get("language", "auto")
                self.engines.append(LocalASR(d, t, m, language=lang))
            except Exception as e:
                log(f"本地引擎初始化失败: {e!r}")

    def transcribe(self, audio_path):
        if not self.engines:
            raise Exception(
                "本地识别引擎不可用\n\n"
                "请确认:\n"
                "  1) pip install sherpa-onnx\n"
                f"  2) 下载模型: {SHERPA_SENSE_VOICE_URL}\n"
                f"  3) 解压到 models/{SHERPA_SENSE_VOICE_MODEL}/\n"
                "  4) 或在设置中指定 model_dir"
            )
        last_err = None
        for eng in self.engines:
            try:
                txt = eng.transcribe(audio_path)
                if txt.strip():
                    state["engine"] = eng.name
                    return txt.strip(), eng.name
            except Exception as e:
                last_err = e
                log(f"{eng.name} 失败: {e}")
        if last_err:
            raise Exception(f"识别失败: {last_err}")
        raise Exception("未识别到内容 (录音可能太短/无声)")

    def count(self):
        return len(self.engines)


# ═══════════════════════════════════════════════════════════
#  音频录制 (优先 WASAPI 独占)
# ═══════════════════════════════════════════════════════════
class AudioRecorder:
    _HIST_LEN = 32  # 频谱条需要的历史帧数

    def __init__(self):
        self.p = pyaudio.PyAudio()
        self.stream = None; self.frames = []; self._active = False
        self._level = 0.0  # 录音音量 (RMS 归一化到 0-1), UI 用
        self._level_history = [0.0] * self._HIST_LEN  # 最近 N 帧 RMS, 给频谱条用
        self._mode = "共享"

    def start(self):
        self.frames = []; self._active = True
        idx = config.get("input_device_index")
        cb = self._cb
        sr = config["sample_rate"]; ch = config["channels"]; cs = config["chunk_size"]

        # 1) 独占开关: 用户在设置中关闭 → 直接走共享模式 (不打扰其他 app)
        # 2) 独占开启 → 先尝试 WASAPI 独占, 失败回退共享
        if config.get("exclusive_device", True):
            self.stream, self._mode = try_open_exclusive_stream(
                self.p, cb, sr, ch, cs, idx)
        else:
            self.stream, self._mode = None, "共享"

        if self.stream is None:
            kw = dict(format=pyaudio.paInt16, channels=ch, rate=sr,
                      input=True, frames_per_buffer=cs, stream_callback=cb)
            if idx is not None: kw["input_device_index"] = idx
            self.stream = self.p.open(**kw)
            self._mode = "共享"

        state["audio_mode"] = self._mode
        log(f"音频: {self._mode}模式")

    def _cb(self, data, fc, ti, st):
        if self._active:
            self.frames.append(data)
            # 计算 RMS 作为实时音量指示 (16-bit PCM, 静音 ≈ 50, 正常说话 1000-5000, 大声 10000+)
            try:
                import array
                samples = array.array('h', data)
                if samples:
                    rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
                    self._level = min(1.0, rms / 6000.0)
                    self._level_history.append(self._level)
                    if len(self._level_history) > self._HIST_LEN:
                        self._level_history.pop(0)
            except Exception:
                pass
        return (None, pyaudio.paContinue)

    def stop(self):
        self._active = False
        if self.stream: self.stream.stop_stream(); self.stream.close(); self.stream = None
        return self.frames

    def save(self, frames, path):
        with wave.open(path, "wb") as wf:
            wf.setnchannels(config["channels"]); wf.setsampwidth(self.p.get_sample_size(pyaudio.paInt16))
            wf.setframerate(config["sample_rate"]); wf.writeframes(b"".join(frames))

    def close(self): self.p.terminate()

    @property
    def mode(self): return self._mode

    @property
    def level(self): return self._level

    @property
    def history(self): return list(self._level_history)

    @staticmethod
    def list_devices():
        """列出所有激活的输入设备, 返回 [(pyaudio_idx, friendly_name, is_default), ...]

        设备名用 IMMDeviceEnumerator 拿 (UTF-16 friendly name, 完全正确),
        而不是 pyaudio.get_device_info_by_index (0.2.14 在 Windows 上对部分
        设备名解码出错, 例如把 "内部 AUX 插座" 显示成 "鍐呴儴 AUX 鎻掑骇").

        关键映射: COM IMMDeviceCollection 的 capture 设备顺序 == pyaudio WASAPI
        host api 中 maxInputChannels>0 设备的顺序 (实测一致). 所以我们可以给每个
        COM device 分配一个对应的 pyaudio global index, 让 AudioRecorder.start()
        仍能照旧用 index 打开流.
        """
        devs = []
        try:
            com_devs = _enum_capture_devices_com()
            if not com_devs:
                # 回退到 pyaudio (COM 失败时)
                p = pyaudio.PyAudio()
                di = p.get_default_input_device_info()["index"]
                for i in range(p.get_device_count()):
                    info = p.get_device_info_by_index(i)
                    if info["maxInputChannels"] > 0:
                        devs.append((i, info["name"], i == di))
                p.terminate()
                return devs

            # 用 pyaudio WASAPI host api 顺序找出 input 设备的 pyaudio global index 列表
            p = pyaudio.PyAudio()
            try:
                # paWASAPI = 13
                wasapi_idx = None
                for i in range(p.get_host_api_count()):
                    if p.get_host_api_info_by_index(i)["type"] == 13:
                        wasapi_idx = i
                        break
                wasapi_input_idxs = []
                if wasapi_idx is not None:
                    wasapi_info = p.get_host_api_info_by_index(wasapi_idx)
                    for hadi in range(wasapi_info["deviceCount"]):
                        info = p.get_device_info_by_host_api_device_index(wasapi_idx, hadi)
                        if info["maxInputChannels"] > 0:
                            wasapi_input_idxs.append(info["index"])
            finally:
                p.terminate()

            # COM 顺序 == WASAPI input 顺序 (已验证)
            for i, (did, name, is_def) in enumerate(com_devs):
                pya_idx = wasapi_input_idxs[i] if i < len(wasapi_input_idxs) else None
                if pya_idx is not None:
                    devs.append((pya_idx, name, is_def))
                else:
                    # 顺序不一致, 回退显示 None index (用户将无法选, 但能看名字)
                    log(f"list_devices: WASAPI input 设备数 {len(wasapi_input_idxs)} < COM 数 {len(com_devs)}, 顺序可能不匹配")
                    break
        except Exception as e:
            log(f"list_devices: {e}\n{traceback.format_exc()}")
        return devs


# ═══════════════════════════════════════════════════════════
#  粘贴
# ═══════════════════════════════════════════════════════════
def paste_text(text: str):
    text = text.strip()
    if not text: return
    try:
        # 先清除剪贴板, 再写入新内容, 避免残留
        pyperclip.copy("")
        time.sleep(0.02)
        pyperclip.copy(text)
        time.sleep(0.04)
        # 用 keyDown/press/keyUp 替代 hotkey, 防止重复触发
        pyautogui.keyDown("ctrl")
        time.sleep(0.02)
        pyautogui.press("v")
        time.sleep(0.02)
        pyautogui.keyUp("ctrl")
        log(f"已粘贴: {text}")
    except Exception as e:
        log(f"粘贴失败: {e}")


# ═══════════════════════════════════════════════════════════
#  录音流程 (带互斥锁防止重复触发)
# ═══════════════════════════════════════════════════════════
_recording_lock = threading.Lock()
_paste_count = 0  # 调试计数器
_current_recorder = None  # 当前录音实例, UI 动画线程从这里读音量 level

def recording_flow():
    global _paste_count, _current_recorder
    rec = None
    mic_guard = None
    mic_guard_active = False
    exclusive_on = config.get("exclusive_device", True)
    state["exclusive"] = exclusive_on
    try:
        rec = AudioRecorder(); asr = ASRManager()
        _current_recorder = rec  # 供 UI 动画读 level
        ui_queue.put(("recording", True))
        # 提示音 (异步播放, 录音线程零阻塞, 不影响 mainloop)
        sound_start()
        ui_queue.put(("status", f"录音中 ({rec.mode}模式)..."))
        log(f"录音中 ({rec.mode}模式)...")

        # 1) 先开音频流, 绑定到具体设备 (PortAudio 内部抓的是物理设备句柄,
        #    不依赖"系统默认设备", 后面 MicGuard 切默认不会影响本流)
        rec.start()

        # 2) 切走系统默认麦克风 (仅在 exclusive_device=True 时)
        #    切走后, 从默认设备取音频的所有 app (Discord/QQ/飞书) 会断流,
        #    拿不到我们说的内容; 松开 Ctrl 立即切回
        if exclusive_on:
            mic_guard = MicGuard()
            try:
                mic_guard.__enter__()
                mic_guard_active = True
                state["mic_guarded"] = True
                log("MicGuard 激活: 系统默认麦克风已切到回退设备")
            except Exception as e:
                log(f"MicGuard 启动失败 (继续录音, 其他 app 可能仍能听到): {e}")
                mic_guard_active = False
                state["mic_guarded"] = False
        else:
            log("独占设备已关闭: 跳过 MicGuard, 其他 app 可正常获取音频")
            state["mic_guarded"] = False
        ui_queue.put(("status", None))  # 触发 _refresh 更新 [独占] 标签

        # 3) 录音循环
        while state["recording"]: time.sleep(0.05)

        # 4) 先恢复 MicGuard (其他 app 立即可用), 再关流
        if mic_guard_active:
            try:
                mic_guard.__exit__(None, None, None)
                log("MicGuard 已恢复默认麦克风")
            except Exception as e:
                log(f"MicGuard 恢复失败: {e}")
            mic_guard_active = False
            state["mic_guarded"] = False
            ui_queue.put(("status", None))

        frames = rec.stop()
        ui_queue.put(("recording", False))
        if not frames or len(frames) < 5:
            ui_queue.put(("error", "录音太短")); return
        ui_queue.put(("status", "识别中...")); log("识别中...")
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            rec.save(frames, tmp.name); apath = tmp.name
        try:
            txt, eng = asr.transcribe(apath)
            log(f"[{eng}] {txt}")
            state["last_text"] = txt; state["last_error"] = ""
            ui_queue.put(("result", txt)); ui_queue.put(("status", f"{eng} ({rec.mode}): {txt}"))
            sound_done()
            _paste_count += 1
            log(f"粘贴第{_paste_count}次")
            paste_text(txt)
        except Exception as e:
            log(f"识别失败: {e}"); state["last_error"] = str(e)
            ui_queue.put(("error", str(e)))
            sound_error()
        finally:
            try: os.unlink(apath)
            except OSError: pass
    except Exception as e:
        log(f"录音错误: {e}\n{traceback.format_exc()}")
        ui_queue.put(("error", f"录音: {e}"))
        sound_error()
    finally:
        # 兜底: 任何路径退出都恢复 MicGuard, 不让系统默认麦克风卡在回退设备
        if mic_guard_active and mic_guard is not None:
            try:
                mic_guard.__exit__(None, None, None)
                state["mic_guarded"] = False
                log("MicGuard 兜底恢复")
            except Exception as e:
                log(f"finally MicGuard 恢复失败: {e}")
        if rec is not None:
            try: rec.close()
            except Exception: pass
        _current_recorder = None  # 清空, UI 动画自动停止
        ui_queue.put(("recording", False))


def start_recording():
    if not _recording_lock.acquire(blocking=False):
        log("录音互斥: 已有录音进行中, 忽略")
        return
    if not state["enabled"]:
        _recording_lock.release(); ui_queue.put(("error", "已禁用")); return
    state["recording"] = True
    threading.Thread(target=_recording_wrapper, daemon=True).start()


def _recording_wrapper():
    try: recording_flow()
    finally: _recording_lock.release()


def stop_recording():
    state["recording"] = False


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
    # 鸟图贴在背景圆上: 64x64 透明底, 先画背景圆, 再居中贴鸟
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, 62, 62], fill=bg)
    bird = base.copy()
    bird.thumbnail((46, 46), Image.LANCZOS)
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

def refresh_tray_icons():
    """品牌图加载完后重新生成托盘 (用于启动时图稍晚于 _icon 初始化)."""
    global ic_idle, ic_rec, ic_off
    ic_idle = _tray_icon_for("idle")
    ic_rec  = _tray_icon_for("recording")
    ic_off  = _tray_icon_for("off")


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
        self._state_cache = None
        self._menu = None
        # 录音脉动: 3 圈, 不同相位
        self._pulses = []   # [(oval_id, phase_offset), ...]
        self._pulse_count = 3
        self._create()

    def _create(self):
        c = self.c
        s = self.SIZE
        self.win = tk.Toplevel(self.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry(f"{s}x{s}")
        try:
            self.win.attributes("-transparentcolor", self._GLASS_KEY)
        except Exception:
            pass  # 降级: 不透明
        self.cv = tk.Canvas(self.win, width=s, height=s,
                            bg=self._GLASS_KEY, highlightthickness=0, cursor="hand2")
        self.cv.pack()
        # 外层阴影 (大圆, 极淡灰, 模拟卡片投影)
        self._shadow = self.cv.create_oval(2, 4, s - 2, s - 0, fill="#e5e7eb", outline="")
        # 玻璃主体: 白色半透明圆
        self._body = self.cv.create_oval(4, 4, s - 4, s - 4, fill="", outline="")
        # 高光层 (玻璃反光, 顶部月牙)
        self._highlight = self.cv.create_oval(12, 6, s - 12, s // 2 + 6,
                                               fill="#f8f8fc", outline="")
        # 品牌图 (中心, 58x58)
        self._photo_id = None
        self._photo_ref = None
        if _brand_img is not None:
            from PIL import ImageTk
            bird = _brand_img.copy()
            bird.thumbnail((46, 46), Image.LANCZOS)
            self._photo_ref = ImageTk.PhotoImage(bird)
            self._photo_id = self.cv.create_image(s // 2, s // 2, image=self._photo_ref)
        # 录音脉冲环 (3 圈, 不同相位)
        pulse_colors = [c["rec"], "#ff6b6b", "#ff8e8e"]
        for i in range(self._pulse_count):
            oval = self.cv.create_oval(0, 0, 0, 0, outline=pulse_colors[i], width=2)
            self.cv.itemconfigure(oval, state="hidden")
            self._pulses.append((oval, i * 0.33))  # 相位偏移 0, 0.33, 0.66
        # 事件
        self.cv.bind("<ButtonPress-1>", self._on_press)
        self.cv.bind("<B1-Motion>", self._on_drag)
        self.cv.bind("<ButtonRelease-1>", self._on_release)
        self.cv.bind("<Button-3>", self._on_right_click)
        self.cv.bind("<Double-Button-1>", lambda e: self._show_main())
        self._menu = tk.Menu(self.win, tearoff=0,
                             bg=c["card"], fg=c["fg"],
                             activebackground=c["accent"], activeforeground="#ffffff",
                             font=("Microsoft YaHei UI", 9), relief=tk.FLAT, bd=1)
        self.win.withdraw()

    # ─────────────── 状态 / 绘制 ───────────────
    def _current_state(self):
        c = self.c
        if not state["enabled"]:
            return ("empty", c["off"], False)       # 禁用: 极淡灰
        if state["recording"]:
            return ("solid", c["rec"], True)         # 录音: 红底 + 脉冲
        return ("glass", c["accent"], False)          # 待命: 玻璃感蓝

    def _redraw(self, force=False):
        mode, color, pulsing = self._current_state()
        sig = (mode, color, pulsing)
        if not force and sig == self._state_cache:
            return
        self._state_cache = sig
        c = self.c
        if mode == "empty":
            self.cv.itemconfigure(self._body, fill="#f0f0f5", outline=c["border"])
            self.cv.itemconfigure(self._highlight, state="hidden")
        elif mode == "solid":
            self.cv.itemconfigure(self._body, fill=color, outline=color)
            self.cv.itemconfigure(self._highlight, state="hidden")
        else:  # glass
            # 玻璃感: 白底 + 80% 不透明 + 带颜色描边 + 高光
            self.cv.itemconfigure(self._body, fill="#ffffff", outline=color, width=2)
            self.cv.itemconfigure(self._highlight, state="normal")
        # 脉冲环: recording 时显示
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

    # ─────────────── 拖动 / 点击 / 右键 ───────────────
    def _on_press(self, e):
        self._drag_data["x"] = e.x_root
        self._drag_data["y"] = e.y_root
        self._drag_data["moved"] = False
        self._trigger_press_anim()

    def _on_drag(self, e):
        dx = e.x_root - self._drag_data["x"]
        dy = e.y_root - self._drag_data["y"]
        if abs(dx) + abs(dy) > self.CLICK_THRESHOLD:
            self._drag_data["moved"] = True
        x = self.win.winfo_x() + dx
        y = self.win.winfo_y() + dy
        self.win.geometry(f"+{x}+{y}")
        self._drag_data["x"] = e.x_root
        self._drag_data["y"] = e.y_root

    def _on_release(self, e):
        # 持久化位置
        x = self.win.winfo_x(); y = self.win.winfo_y()
        config["bubble_x"] = x; config["bubble_y"] = y
        save_config()
        # 没拖动 = 点击 → 恢复主窗口
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
        # 位置: 持久化坐标 > 默认 (屏幕右侧中部)
        x = config.get("bubble_x")
        y = config.get("bubble_y")
        if x is None or y is None:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            x = sw - self.SIZE - 30
            y = sh // 2 - self.SIZE // 2
        # 边界保护: 至少 5px 在屏内
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
                          fg=c["btn_fg"], bg=c["accent"], activebackground="#0055aa",
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
#  主界面 (v0.6.0 浅色系: 白底 + 浅灰 + 蓝/绿强调)
# ═══════════════════════════════════════════════════════════
class MainWindow:
    """v0.6.1 扁平化重设计:
    - 调色板: 5 个核心色, 降饱和度
    - 录音按钮: 单色实心圆, 中心用品牌图 (assets/app_icon.png) 替代 emoji
    - 取消脉冲环 / 呼吸光 / 频谱条 — 扁平化不喜装饰动效
    - 状态展示: 大字号 "待命 / 正在录音 / 已暂停" + 小字副标题
    - 顶栏 44px, 底栏 28px (更克制)
    - 微动效: 仅按下态 scale 0.97 + 100ms 缓出
    """
    # 录音按钮 Canvas 200x200, 主体圆 80px 半径
    _RB_SIZE = 200
    _RB_CX, _RB_CY = 100, 100
    _RB_R = 80  # 主体圆半径 (直径 160)

    def __init__(self, tray_ref, start_minimized=False):
        self.tray = tray_ref
        self.root = tk.Tk()
        self.root.title("言栖")
        # 窗口图标: frozen 模式从 _MEIPASS 取, 开发模式从工作目录取
        ico_path = _resolve_asset_path("app.ico")
        if ico_path and os.path.exists(ico_path):
            try: self.root.iconbitmap(default=ico_path)
            except Exception: pass
        # v0.6.1 扁平化调色板 — 降饱和, 5 个核心色
        self.c = {
            "bg":      "#fafafa",  # 主背景 (微暖白)
            "card":    "#ffffff",  # 卡片底
            "border":  "#e5e7eb",  # 1px 极淡边
            "border2": "#d1d5db",  # 输入框/按钮描边
            "fg":      "#111827",  # 主文字 (近黑)
            "fg2":     "#6b7280",  # 次级文字
            "fg3":     "#9ca3af",  # 三级 (hint)
            "accent":  "#3b5bdb",  # 主蓝 (偏蓝紫)
            "rec":     "#dc2626",  # 录音红
            "ok":      "#16a34a",  # 成功绿
            "warn":    "#f59e0b",  # 警告橙
            "off":     "#9ca3af",  # 禁用灰
            "btn_fg":  "#ffffff",  # 按钮文字白
        }
        # 定位屏幕右下角
        sw = self.root.winfo_screenwidth(); sh = self.root.winfo_screenheight()
        w, h = 360, 520
        self.root.geometry(f"{w}x{h}+{sw - w - 60}+{sh - h - 100}")
        self.root.resizable(True, True); self.root.minsize(340, 460)
        self.root.configure(bg=self.c["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if start_minimized:
            self.root.withdraw()  # 开机启动时直接进托盘, 不弹主窗口
        # 动画状态 — v0.6.1 简化: 仅按下态 scale 缓动, 无装饰动效
        self._rec_btn_pressed = False
        self._press_scale = 1.0  # 按下态 scale, 1.0 = 无
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
        # 加品牌图
        photo = self._pil_to_photo(self._brand_resized(96))
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

    # ─────────────── 构建 UI ───────────────
    def _build(self):
        c = self.c
        # 1) 底栏 (28px, ghost 文字按钮, 极简) — 先 BOTTOM pack
        bot = tk.Frame(self.root, bg=c["bg"], height=32)
        bot.pack(side=tk.BOTTOM, fill=tk.X)
        bot.pack_propagate(False)
        self.copy_btn = self._mk_ghost(bot, "复制", self._copy_result)
        self.copy_btn.pack(side=tk.RIGHT, padx=(0, 14), pady=4)
        self._mk_ghost(bot, "设置",
                       lambda: SettingsDialog(self.root, self)).pack(side=tk.RIGHT, pady=4)
        self._mk_ghost(bot, "关于", self._about).pack(side=tk.RIGHT, pady=4)
        tk.Label(bot, text="v0.5.0", font=("Consolas", 8), fg=c["fg3"], bg=c["bg"]).pack(side=tk.LEFT, padx=14, pady=10)

        # 2) 顶栏 (44px, 极简, 无分隔线)
        top = tk.Frame(self.root, bg=c["bg"], height=44)
        top.pack(side=tk.TOP, fill=tk.X)
        top.pack_propagate(False)
        # 左: 品牌名 (无 logo, 纯文字)
        tk.Label(top, text="言栖", font=("Microsoft YaHei UI", 13, "bold"),
                 fg=c["fg"], bg=c["bg"]).pack(side=tk.LEFT, padx=(16, 0), pady=12)
        # 右: 模式徽章 + 启用文字 (替代旧 Button)
        right = tk.Frame(top, bg=c["bg"])
        right.pack(side=tk.RIGHT, padx=(0, 14), pady=10)
        self.mode_lbl = tk.Label(right, text="", font=("Microsoft YaHei UI", 8),
                                 fg=c["fg3"], bg=c["bg"])
        self.mode_lbl.pack(side=tk.RIGHT, padx=(8, 0))
        self.tgl_lbl = tk.Label(right, text="启用", font=("Microsoft YaHei UI", 9),
                                fg=c["accent"], bg=c["bg"], cursor="hand2", padx=8, pady=2)
        self.tgl_lbl.pack(side=tk.RIGHT)
        self.tgl_lbl.bind("<Button-1>", lambda e: self._toggle())

        # 3) 中部: 录音按钮 (200x200 Canvas, 居中, 上方留白)
        rec_frame = tk.Frame(self.root, bg=c["bg"])
        rec_frame.pack(side=tk.TOP, fill=tk.X, pady=(28, 0))
        self.rbtn_cv = tk.Canvas(rec_frame, width=self._RB_SIZE, height=self._RB_SIZE,
                                 bg=c["bg"], highlightthickness=0, cursor="hand2")
        self.rbtn_cv.pack()
        # 主体圆 (扁平: 无脉冲/无呼吸)
        self._rbtn_circle_id = self.rbtn_cv.create_oval(
            self._RB_CX - self._RB_R, self._RB_CY - self._RB_R,
            self._RB_CX + self._RB_R, self._RB_CY + self._RB_R,
            fill=c["card"], outline=c["border2"], width=1.5)
        # 录音态 ■ (实心方块) 初始隐藏
        self._rec_square_id = self.rbtn_cv.create_rectangle(
            self._RB_CX - 18, self._RB_CY - 18,
            self._RB_CX + 18, self._RB_CY + 18,
            fill=c["btn_fg"], outline="")
        self.rbtn_cv.itemconfigure(self._rec_square_id, state="hidden")
        # 中心品牌图: 优先用 _brand_img, 缺失时降级到 ⌜□⌝ 占位文字
        self._rbtn_photo_id = None
        self._rbtn_photo_ref = None
        self._rbtn_placeholder_text = None
        if _brand_img is not None:
            self._refresh_brand()
        else:
            # 占位: 一个 ⌜⌝ 形状 (扁平风, 等用户存图后会被覆盖)
            self._rbtn_placeholder_text = self.rbtn_cv.create_text(
                self._RB_CX, self._RB_CY, text="◐", font=("Segoe UI Symbol", 48),
                fill=c["accent"])
        # 事件
        self.rbtn_cv.bind("<ButtonPress-1>", lambda e: self._on_press())
        self.rbtn_cv.bind("<ButtonRelease-1>", lambda e: self._on_release())
        self.root.bind("<ButtonRelease-1>", self._root_release, add="+")

        # 4) 状态文字: 大字号 "待命" + 小字号副标题
        status_frame = tk.Frame(self.root, bg=c["bg"])
        status_frame.pack(side=tk.TOP, fill=tk.X, pady=(20, 0))
        self.lbl_main = tk.Label(status_frame, text="待命", font=("Microsoft YaHei UI", 18, "bold"),
                                 fg=c["fg"], bg=c["bg"])
        self.lbl_main.pack()
        self.lbl_sub = tk.Label(status_frame, text="按住  Right Ctrl  开始录音",
                                font=("Microsoft YaHei UI", 9), fg=c["fg2"], bg=c["bg"])
        self.lbl_sub.pack(pady=(4, 0))

        # 5) 识别结果区 (expand=True 占满中间)
        res_frame = tk.Frame(self.root, bg=c["bg"])
        res_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=16, pady=(20, 8))
        # 文本卡片: 白底 + 1px 边
        text_container = tk.Frame(res_frame, bg=c["card"], bd=0,
                                   highlightthickness=1, highlightbackground=c["border"])
        text_container.pack(fill=tk.BOTH, expand=True)
        self.txt = tk.Text(text_container, font=("Microsoft YaHei UI", 12),
                           fg=c["fg"], bg=c["card"], relief=tk.FLAT,
                           wrap=tk.WORD, borderwidth=0, padx=14, pady=12,
                           insertbackground=c["accent"], spacing1=2, spacing3=2,
                           height=6, takefocus=0)
        scroll = tk.Scrollbar(text_container, command=self.txt.yview, bd=0,
                              bg=c["card"], troughcolor=c["card"],
                              activebackground=c["border"], width=4)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.txt.configure(yscrollcommand=scroll.set)
        self.txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.txt.tag_configure("title", font=("Microsoft YaHei UI", 13, "bold"),
                               foreground=c["fg2"], spacing1=2, spacing3=2)
        self.txt.tag_configure("kbd", font=("Cascadia Mono", 9, "bold"),
                               foreground=c["accent"],
                               background="#eef2ff", spacing1=0, spacing3=0)
        self.txt.tag_configure("result", font=("Microsoft YaHei UI", 12),
                               foreground=c["fg"], lmargin1=2, lmargin2=2)
        self.txt.tag_configure("error", font=("Microsoft YaHei UI", 11),
                               foreground=c["rec"])
        self.txt.configure(state=tk.DISABLED)
        self.root.after_idle(self._set_text_placeholder)
        self._engine_text = "本地 SenseVoice"

    def _mk_ghost(self, parent, text, cmd):
        """底栏 ghost 文字按钮: 默认次级灰, hover 主文字色."""
        c = self.c
        def on_enter(e): btn.configure(fg=c["fg"])
        def on_leave(e): btn.configure(fg=c["fg2"])
        btn = tk.Label(parent, text=text, font=("Microsoft YaHei UI", 9),
                       fg=c["fg2"], bg=c["bg"], cursor="hand2", padx=8, pady=2)
        btn.bind("<Button-1>", lambda e: cmd())
        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)
        return btn

    def _mk_nav_btn(self, parent, text, cmd):
        """底部导航按钮: 浅色文字按钮, hover 时变色"""
        c = self.c
        return tk.Button(parent, text=text, font=("Microsoft YaHei UI", 9),
                         fg=c["fg2"], bg=c["bg"], activebackground=c["card"],
                         activeforeground=c["fg"], relief=tk.FLAT, cursor="hand2",
                         bd=0, highlightthickness=0, padx=8, pady=2, command=cmd)

    def _set_text_placeholder(self):
        self.txt.configure(state=tk.NORMAL)
        self.txt.delete("1.0", tk.END)
        self.txt.insert("1.0", "等待语音输入", "title")
        self.txt.insert("end", "\n\n按住  ")
        self.txt.insert("end", "Right Ctrl", "kbd")
        self.txt.insert("end", "  说话")
        self.txt.insert("end", "切换  ")
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
        """v0.6.1 扁平化切换: idle 白底灰边, 录音 红色实心 + 白方块."""
        c = self.c
        if on:
            self.rbtn_cv.itemconfigure(self._rbtn_circle_id, fill=c["rec"], outline="")
            self.rbtn_cv.itemconfigure(self._rec_square_id, state="normal")
            if self._rbtn_photo_id is not None:
                self.rbtn_cv.itemconfigure(self._rbtn_photo_id, state="hidden")
            if self._rbtn_placeholder_text is not None:
                self.rbtn_cv.itemconfigure(self._rbtn_placeholder_text, state="hidden")
        else:
            self.rbtn_cv.itemconfigure(self._rbtn_circle_id, fill=c["card"], outline=c["border2"])
            self.rbtn_cv.itemconfigure(self._rec_square_id, state="hidden")
            if self._rbtn_photo_id is not None:
                self.rbtn_cv.itemconfigure(self._rbtn_photo_id, state="normal")
            if self._rbtn_placeholder_text is not None:
                self.rbtn_cv.itemconfigure(self._rbtn_placeholder_text, state="normal")

    def _animate(self):
        """v0.6.1: 仅按下态 scale 缓出. 无其他动效."""
        if self._rec_btn_pressed and self._press_scale > 0.97:
            self._press_scale = max(0.97, self._press_scale - 0.04)
            self._apply_press_scale(self._press_scale)
        elif not self._rec_btn_pressed and self._press_scale < 1.0:
            self._press_scale = min(1.0, self._press_scale + 0.05)
            self._apply_press_scale(self._press_scale)
        self.root.after(40, self._animate)

    def _apply_press_scale(self, scale):
        r = self._RB_R * scale
        self.rbtn_cv.coords(self._rbtn_circle_id,
                            self._RB_CX - r, self._RB_CY - r,
                            self._RB_CX + r, self._RB_CY + r)

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
            self.tgl_lbl.configure(text="停止", fg=c["rec"])
            self._set_rec_visual(True)
        elif state["enabled"]:
            self.lbl_main.configure(text="待命", fg=c["fg"])
            self.lbl_sub.configure(text="按住  Right Ctrl  开始录音", fg=c["fg2"])
            self.tgl_lbl.configure(text="启用", fg=c["accent"])
            self._set_rec_visual(False)
        else:
            self.lbl_main.configure(text="已禁用", fg=c["fg3"])
            self.lbl_sub.configure(text="点击右上角启用恢复  ·  Ctrl+Shift+F9", fg=c["fg3"])
            self.tgl_lbl.configure(text="已禁用", fg=c["fg3"])
            self._set_rec_visual(False)

    def _poll(self):
        try:
            while True:
                m = ui_queue.get_nowait()
                k = m[0]
                if k == "recording": self._refresh(); self._update_tray()
                elif k == "result":
                    self.txt.configure(state=tk.NORMAL)
                    self.txt.delete("1.0", tk.END)
                    self.txt.insert("1.0", m[1], "result")
                    self.txt.configure(state=tk.DISABLED)
                elif k == "error":
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

    def _copy_result(self):
        try:
            content = self.txt.get("1.0", tk.END).strip()
            if content and content != "等待语音输入":
                pyperclip.copy(content)
                old = self.copy_btn.cget("text")
                self.copy_btn.configure(text="已复制", fg=self.c["accent"])
                self.root.after(1200, lambda: self.copy_btn.configure(text=old, fg=self.c["fg2"]))
                log(f"复制结果: {content[:30]}...")
        except Exception as e:
            log(f"复制失败: {e}")

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
        self.win.transient(parent)
        self.win.attributes("-topmost", True)
        main_win._settings_win = self.win
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw = parent.winfo_width()
            w, h = 500, 520
            self.win.geometry(f"{w}x{h}+{px + pw + 8}+{py}")
        except Exception:
            self.win.geometry("500x520")
        self.win.update_idletasks()
        self.win.grab_set()
        # ---- sidebar nav (no ttk.Notebook - avoids rendering bugs) ----
        top = tk.Frame(self.win, bg=c["bg"], height=44)
        top.pack(side=tk.TOP, fill=tk.X)
        top.pack_propagate(False)
        tk.Label(top, text="设置", font=("Microsoft YaHei UI", 14, "bold"),
                 fg=c["fg"], bg=c["bg"]).pack(side=tk.LEFT, padx=16, pady=10)
        tk.Frame(self.win, bg=c["border"], height=1).pack(side=tk.TOP, fill=tk.X)
        body = tk.Frame(self.win, bg=c["bg"])
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        sidebar = tk.Frame(body, bg=c["bg"], width=120)
        sidebar.pack(side=tk.LEFT, fill=tk.Y)
        sidebar.pack_propagate(False)
        sidebar_inner = tk.Frame(sidebar, bg=c["bg"])
        sidebar_inner.pack(fill=tk.X, padx=0, pady=8)
        tk.Frame(body, bg=c["border"], width=1).pack(side=tk.LEFT, fill=tk.Y)
        self._content_frame = tk.Frame(body, bg=c["bg"])
        self._content_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # nav items: Canvas-drawn buttons with left accent bar
        self._nav_items = {}
        self._nav_keys = {}
        self._nav_active = None
        for key, label in [("general", "通用"), ("audio", "音频设备")]:
            nav_cv = tk.Canvas(sidebar_inner, width=118, height=36,
                               bg=c["bg"], highlightthickness=0, cursor="hand2")
            nav_cv.pack(pady=1)
            bar = nav_cv.create_rectangle(0, 8, 3, 28, fill="", outline="")
            txt = nav_cv.create_text(16, 18, text=label, anchor=tk.W,
                                     font=("Microsoft YaHei UI", 10), fill=c["fg2"])
            nav_cv.bind("<Button-1>", self._nav_click)
            nav_cv.bind("<Enter>", self._nav_hover_on)
            nav_cv.bind("<Leave>", self._nav_hover_off)
            self._nav_keys[nav_cv] = key
            self._nav_items[key] = (nav_cv, bar, txt)
        # pre-build pages
        self._pages = {}
        self._pages["general"] = self._general_tab(self._content_frame)
        self._pages["audio"] = self._audio_tab(self._content_frame)
        for f in self._pages.values():
            f.pack_forget()
        self._switch_tab("general")
        # bottom bar
        tk.Frame(self.win, bg=c["border"], height=1).pack(side=tk.TOP, fill=tk.X)
        bot = tk.Frame(self.win, bg=c["bg"])
        bot.pack(side=tk.TOP, fill=tk.X, padx=16, pady=8)
        tk.Label(bot, text="设置修改后立即生效", font=("Microsoft YaHei UI", 8),
                 fg=c["fg3"], bg=c["bg"]).pack(side=tk.LEFT)
        close_btn = tk.Button(bot, text="完成", font=("Microsoft YaHei UI", 9, "bold"),
                              fg=c["btn_fg"], bg=c["accent"], activebackground="#0055aa",
                              activeforeground=c["btn_fg"], relief=tk.FLAT, cursor="hand2",
                              bd=0, highlightthickness=0, padx=18, pady=4,
                              command=self._on_close)
        close_btn.pack(side=tk.RIGHT)

    def _nav_click(self, event):
        key = self._nav_keys.get(event.widget)
        if key: self._switch_tab(key)

    def _nav_hover_on(self, event):
        key = self._nav_keys.get(event.widget)
        if key and self._nav_active != key:
            cv = self._nav_items[key][0]
            txt = self._nav_items[key][2]
            cv.itemconfigure(txt, fill=self.mw.c["fg"])

    def _nav_hover_off(self, event):
        key = self._nav_keys.get(event.widget)
        if key and self._nav_active != key:
            cv = self._nav_items[key][0]
            txt = self._nav_items[key][2]
            cv.itemconfigure(txt, fill=self.mw.c["fg2"])

    def _switch_tab(self, key):
        c = self.mw.c
        for k, (cv, bar, txt) in self._nav_items.items():
            cv.itemconfigure(bar, fill="")
            cv.itemconfigure(txt, fill=c["fg2"], font=("Microsoft YaHei UI", 10))
            cv.configure(bg=c["bg"])
        if key in self._nav_items:
            cv, bar, txt = self._nav_items[key]
            cv.configure(bg=c["card"])
            cv.itemconfigure(bar, fill=c["accent"])
            cv.itemconfigure(txt, fill=c["fg"], font=("Microsoft YaHei UI", 10, "bold"))
            self._nav_active = key
        for k, f in self._pages.items():
            if k == key:
                f.pack(fill=tk.BOTH, expand=True, padx=(16, 16), pady=(4, 0))
            else:
                f.pack_forget()

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

    def _setting_row(self, parent, title, description, var, on_toggle, on_color="accent"):
        """一行设置: 标题 + 描述 + Switch 控件 (v0.6.1 Canvas 自绘).
        返回 (frame, var) 以便调用方后续读取 var 状态."""
        c = self.mw.c
        card = tk.Frame(parent, bg=c["card"], highlightthickness=1, highlightbackground=c["border"])
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
        f = tk.Frame(p, bg=c["bg"])
        f.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
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
            status = "已注册到 HKCU\\...\\Run" if is_auto_start_enabled() else "未注册"
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

        # 悬浮气泡 (v0.7.0 预留)
        self._section_label(f, "桌面", "关闭主窗口后是否留在桌面上")
        self.bubble_var = tk.BooleanVar(value=config.get("floating_bubble", True))
        self._setting_row(f, "悬浮气泡",
                          "关闭主窗口后, 在桌面显示一个小气泡\n"
                          "气泡状态反映当前功能 (待命/录音中/已关闭)",
                          self.bubble_var, self._bubble_toggle)

        # 关于区块
        self._section_label(f, "关于", "")
        info_card = tk.Frame(f, bg=c["card"], highlightthickness=1, highlightbackground=c["border"])
        info_card.pack(fill=tk.X, pady=4)
        tk.Label(info_card, text="言栖 v0.5.0 (pre-release)", font=("Microsoft YaHei UI", 10, "bold"),
                 fg=c["fg"], bg=c["card"]).pack(anchor=tk.W, padx=14, pady=(10, 0))
        tk.Label(info_card, text="本地离线识别 · sherpa-onnx + SenseVoice",
                 font=("Microsoft YaHei UI", 8), fg=c["fg2"], bg=c["card"]).pack(anchor=tk.W, padx=14, pady=(2, 0))
        link = tk.Label(info_card, text="github.com/Xinyang-S/STT-YanQi",
                        font=("Consolas", 8), fg=c["accent"], bg=c["card"], cursor="hand2")
        link.pack(anchor=tk.W, padx=14, pady=(2, 10))
        return f

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
        f = tk.Frame(p, bg=c["bg"])
        f.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
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
                                 highlightbackground=c["border"])
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
        return f

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
