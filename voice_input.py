#!/usr/bin/env python3
"""
言栖 (Yán Qī) — Voice Input for AI Agents (v5.0)
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
_WAV_START     = _gen_tone([(880, 0.05), (1200, 0.06)])   # 上升: 类似 unmute
_WAV_DONE      = _gen_tone([(1568, 0.10)])                 # 叮: 类似 DM 通知
_WAV_TOGGLE_ON  = _gen_tone([(1047, 0.06)])                # 轻响
_WAV_TOGGLE_OFF = _gen_tone([(784, 0.06)])
_WAV_ERROR     = _gen_tone([(262, 0.15)])                 # 低沉
_WAV_FALLBACK  = _gen_tone([(660, 0.04), (660, 0.04)])    # 双声

try:
    import winsound
    # winsound 限制: SND_MEMORY 不允许 SND_ASYNC (Python 包装器会抛 RuntimeError)
    # 真异步唯一靠谱的方案是写临时文件 + SND_FILENAME | SND_ASYNC.
    # _play 用线程 + 临时文件: 调用方零阻塞, 异步播放, 文件自动清理, 任何异常都进日志.
    def _play(wav, blocking_ms=300):
        """非阻塞播放 (主线程零阻塞, 异步播出).
        blocking_ms: 临时文件保留时长, 必须 >= WAV 时长, 否则 PlaySound 中途会因文件被删而静音."""
        def _worker():
            path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    f.write(wav); path = f.name
                # SND_NODEFAULT: 找不到时不响系统默认音, 避免覆盖我们的提示音
                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
                time.sleep(blocking_ms / 1000)  # 异步播完再删文件 (PlaySound 立即返回)
            except Exception as e:
                log(f"音效播放失败: {e!r}")
            finally:
                if path:
                    try: os.unlink(path)
                    except OSError: pass
        threading.Thread(target=_worker, daemon=True).start()
    def _play_sync(wav):
        """阻塞播放: 调用方等播完 (保留 API 兼容性, 现在改用 _play 异步)."""
        _play(wav, blocking_ms=300)
except ImportError:
    def _play(wav, blocking_ms=300):  return
    def _play_sync(wav):  return

# 所有音效统一为异步, 不再使用 SND_MEMORY (PyInstaller --windowed 下 SND_MEMORY
# 偶发失败: PlaySound 返回但 Windows 音频子系统未真正播放, 原因是内存指针生命周期
# 跨线程的问题). 临时文件 + SND_ASYNC 方案更稳.
def sound_start():      _play(_WAV_START,  blocking_ms=200)
def sound_done():       _play(_WAV_DONE,   blocking_ms=200)
def sound_toggle_on():  _play(_WAV_TOGGLE_ON)
def sound_toggle_off(): _play(_WAV_TOGGLE_OFF)
def sound_error():      _play(_WAV_ERROR)
def sound_fallback():   _play(_WAV_FALLBACK)


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
#  托盘图标
# ═══════════════════════════════════════════════════════════
def _icon(c):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([4, 4, 60, 60], fill=c)
    d.ellipse([16, 12, 36, 32], fill=(255, 255, 255, 60))
    return img

G, R, A, Y = (39, 174, 96), (231, 76, 60), (149, 165, 166), (243, 156, 18)
ic_idle, ic_rec, ic_off, ic_fb = _icon(G), _icon(R), _icon(A), _icon(Y)

def get_tray_icon():
    if state["recording"]: return ic_rec
    if not state["enabled"]: return ic_off
    if state["engine"] == "本地": return ic_idle
    return ic_idle


# ═══════════════════════════════════════════════════════════
#  主界面
# ═══════════════════════════════════════════════════════════
class MainWindow:
    # 录音按钮中心 (Canvas 140x140)
    _RB_CX, _RB_CY = 70, 70
    _RB_R = 24  # 中心圆基础半径
    _RB_PULSE = (35, 50, 65)  # 三圈脉冲环基础半径 (录音态)
    _RB_BREATH = 28  # 呼吸光外圈基础半径
    # 频谱条 (Canvas 220x32)
    _SP_BARS = 32
    _SP_W = 220
    _SP_BAR_W = 3
    _SP_GAP = 2
    _SP_MAX_H = 22

    def __init__(self, tray_ref, start_minimized=False):
        self.tray = tray_ref
        self.root = tk.Tk()
        self.root.title("言栖")
        # Tokyo Night 配色 + 录音态辅助色
        self.c = {
            "bg":     "#1a1b26", "bg2":  "#16161e", "card": "#24283b",
            "card2":  "#2a2f45", "border": "#414868", "fg":   "#c0caf5",
            "fg2":    "#9aa5ce", "fg3":  "#565f89", "accent": "#7aa2f7",
            "accent2":"#bb9af7", "ok":   "#9ece6a", "warn": "#e0af68",
            "err":    "#f7768e", "rec":  "#f7768e", "rec_glow": "#ff79c6",
            "ok_dim": "#5a7a3a",
        }
        # 定位屏幕右下角
        sw = self.root.winfo_screenwidth(); sh = self.root.winfo_screenheight()
        w, h = 380, 580
        self.root.geometry(f"{w}x{h}+{sw - w - 60}+{sh - h - 100}")
        self.root.resizable(True, True); self.root.minsize(360, 500)
        self.root.configure(bg=self.c["bg"])
        self.root.protocol("WM_DELETE_WINDOW", lambda: self.root.withdraw())
        if start_minimized:
            self.root.withdraw()  # 开机启动时直接进托盘, 不弹主窗口
        # 动画状态
        self._level_smooth = 0.0
        self._rec_btn_pressed = False
        self._press_anim = None  # 按下动画: 帧序列 [(t_offset, radius), ...]
        self._press_anim_t = 0
        self._build(); self._poll(); self._animate()

    # ─────────────── 构建 UI ───────────────
    def _build(self):
        c = self.c
        # ── 底栏 (先 BOTTOM pack) ──
        bot = tk.Frame(self.root, bg=c["bg"])
        bot.pack(side=tk.BOTTOM, fill=tk.X, padx=20, pady=(10, 14))
        tk.Label(bot, text="v5.0", font=("Consolas", 8), fg=c["fg3"], bg=c["bg"]).pack(side=tk.LEFT)
        self.copy_btn = self._mk_nav_btn(bot, "📋  复制", self._copy_result)
        self.copy_btn.pack(side=tk.RIGHT, padx=(6, 0))
        self._mk_nav_btn(bot, "⚙  设置",
                         lambda: SettingsDialog(self.root, self)).pack(side=tk.RIGHT, padx=(6, 0))
        self._mk_nav_btn(bot, "关于", self._about).pack(side=tk.RIGHT)

        # ── 顶栏 (TOP pack, 固定 56px) ──
        top = tk.Frame(self.root, bg=c["bg2"], height=56)
        top.pack(side=tk.TOP, fill=tk.X)
        top.pack_propagate(False)
        left = tk.Frame(top, bg=c["bg2"])
        left.pack(side=tk.LEFT, padx=18)
        self.dot_cv = tk.Canvas(left, width=14, height=14, bg=c["bg2"], highlightthickness=0)
        self.dot_cv.pack(side=tk.LEFT, padx=(0, 8), pady=21)
        self.dot_id = self.dot_cv.create_oval(2, 2, 12, 12, fill=c["ok"], outline="")
        self.lbl = tk.Label(left, text="待命", font=("Microsoft YaHei UI", 10, "bold"),
                            fg=c["fg"], bg=c["bg2"])
        self.lbl.pack(side=tk.LEFT, pady=18)
        right = tk.Frame(top, bg=c["bg2"])
        right.pack(side=tk.RIGHT, padx=14)
        self.tgl = tk.Button(right, text="  禁用  ", font=("Microsoft YaHei UI", 9),
                             fg=c["fg2"], bg=c["card"], activebackground=c["card2"],
                             activeforeground=c["fg"], relief=tk.FLAT, cursor="hand2",
                             bd=0, highlightthickness=0, padx=4, command=self._toggle)
        self.tgl.pack(side=tk.RIGHT, padx=(8, 0), pady=14)
        self.mode_lbl = tk.Label(right, text="", font=("Microsoft YaHei UI", 8),
                                 fg=c["fg3"], bg=c["bg2"])
        self.mode_lbl.pack(side=tk.RIGHT, pady=20)

        # ── 录音区 (TOP pack, 固定高度) ──
        rec_frame = tk.Frame(self.root, bg=c["bg"])
        rec_frame.pack(side=tk.TOP, fill=tk.X, pady=(4, 0))
        # 录音按钮 Canvas (140 半径 24, 三圈脉冲 35/50/65)
        self.rbtn_cv = tk.Canvas(rec_frame, width=140, height=140, bg=c["bg"],
                                 highlightthickness=0, cursor="hand2")
        self.rbtn_cv.pack()
        # 3 圈脉冲环 (录音时显示)
        self._pulses = []
        for base_r in self._RB_PULSE:
            oval = self.rbtn_cv.create_oval(
                self._RB_CX - base_r, self._RB_CY - base_r,
                self._RB_CX + base_r, self._RB_CY + base_r,
                outline=c["rec_glow"], width=1, fill="")
            self.rbtn_cv.itemconfigure(oval, state="hidden")
            self._pulses.append(oval)
        # 呼吸光外圈
        self._breath = self.rbtn_cv.create_oval(
            self._RB_CX - self._RB_BREATH, self._RB_CY - self._RB_BREATH,
            self._RB_CX + self._RB_BREATH, self._RB_CY + self._RB_BREATH,
            outline=c["rec_glow"], width=2, fill="")
        self.rbtn_cv.itemconfigure(self._breath, state="hidden")
        # 主体圆
        self.rbtn_circle = self.rbtn_cv.create_oval(
            self._RB_CX - self._RB_R, self._RB_CY - self._RB_R,
            self._RB_CX + self._RB_R, self._RB_CY + self._RB_R,
            fill=c["ok"], outline="")
        # 中心图标
        self.rbtn_icon = self.rbtn_cv.create_text(
            self._RB_CX, self._RB_CY, text="🎙",
            font=("Segoe UI Emoji", 24), fill=c["bg"])
        # 事件
        self.rbtn_cv.bind("<ButtonPress-1>", lambda e: self._on_press())
        self.rbtn_cv.bind("<ButtonRelease-1>", lambda e: self._on_release())
        self.root.bind("<ButtonRelease-1>", self._root_release, add="+")

        # 提示行: "按住说话" + 快捷键 pill
        hint_row = tk.Frame(rec_frame, bg=c["bg"])
        hint_row.pack(pady=(12, 0))
        self.rbtn_hint = tk.Label(hint_row, text="按住说话",
                                  font=("Microsoft YaHei UI", 10), fg=c["fg2"], bg=c["bg"])
        self.rbtn_hint.pack(side=tk.LEFT, padx=(0, 6))
        self.key_pill = tk.Label(hint_row, text=" Right Ctrl ",
                                 font=("Cascadia Mono", 9, "bold"),
                                 fg=c["accent"], bg=c["card"],
                                 padx=8, pady=2)
        self.key_pill.pack(side=tk.LEFT)

        # 频谱条 (录音时显示, idle 时隐藏)
        self.sp_cv = tk.Canvas(rec_frame, width=self._SP_W, height=36, bg=c["bg"],
                               highlightthickness=0, bd=0)
        self.sp_cv.pack(pady=(10, 0))
        sp_total = self._SP_BARS * self._SP_BAR_W + (self._SP_BARS - 1) * self._SP_GAP
        sp_x0 = (self._SP_W - sp_total) / 2
        self._sp_bars = []
        for i in range(self._SP_BARS):
            x = sp_x0 + i * (self._SP_BAR_W + self._SP_GAP)
            bar = self.sp_cv.create_rectangle(x, 18, x + self._SP_BAR_W, 18,
                                              fill=c["fg3"], outline="")
            self.sp_cv.itemconfigure(bar, state="hidden")
            self._sp_bars.append(bar)

        # ── 识别结果区 (最后 pack, expand=True 占满中间) ──
        res_frame = tk.Frame(self.root, bg=c["bg"])
        res_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=20, pady=(12, 0))
        res_title = tk.Frame(res_frame, bg=c["bg"])
        res_title.pack(fill=tk.X)
        tk.Label(res_title, text="识别结果", font=("Microsoft YaHei UI", 9, "bold"),
                 fg=c["fg2"], bg=c["bg"]).pack(side=tk.LEFT)
        self.eng_badge = tk.Label(res_title, text="", font=("Microsoft YaHei UI", 8, "bold"),
                                  fg=c["ok"], bg=c["card"], padx=8, pady=2)
        self.eng_badge.pack(side=tk.RIGHT)
        text_container = tk.Frame(res_frame, bg=c["card"], bd=0,
                                   highlightthickness=1, highlightbackground=c["border"])
        text_container.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.txt = tk.Text(text_container, font=("Microsoft YaHei UI", 11),
                           fg=c["fg"], bg=c["card"], relief=tk.FLAT,
                           wrap=tk.CHAR, borderwidth=0, padx=12, pady=10,
                           insertbackground=c["accent"], spacing1=2, spacing3=2,
                           height=8, takefocus=0)
        # 滚动条 (解决 winfo_height 与实际渲染不一致的 bug, 让 text 内部能滚)
        scroll = tk.Scrollbar(text_container, command=self.txt.yview, bd=0,
                              bg=c["card2"], troughcolor=c["card"],
                              activebackground=c["border"], width=6)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.txt.configure(yscrollcommand=scroll.set)
        self.txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # 富文本 tag
        self.txt.tag_configure("title", font=("Microsoft YaHei UI", 13, "bold"),
                               foreground=c["accent"], spacing1=4, spacing3=4)
        self.txt.tag_configure("kbd", font=("Cascadia Mono", 9, "bold"),
                               foreground=c["accent"],
                               background=c["bg2"], spacing1=0, spacing3=0)
        self.txt.tag_configure("result", font=("Microsoft YaHei UI", 12),
                               foreground=c["fg"], lmargin1=2, lmargin2=2)
        self.txt.tag_configure("error", font=("Microsoft YaHei UI", 11),
                               foreground=c["err"])
        self.txt.configure(state=tk.DISABLED)
        # 延迟设置 placeholder, 等 text widget 完成布局后再写 (避免 width=0 时 wrap)
        self.root.after_idle(self._set_text_placeholder)

    def _mk_nav_btn(self, parent, text, cmd):
        c = self.c
        return tk.Button(parent, text=text, font=("Microsoft YaHei UI", 9),
                         fg=c["fg2"], bg=c["bg"], activebackground=c["card"],
                         activeforeground=c["fg"], relief=tk.FLAT, cursor="hand2",
                         bd=0, highlightthickness=0, padx=8, pady=2, command=cmd)

    def _set_text_placeholder(self):
        self.txt.configure(state=tk.NORMAL)
        self.txt.delete("1.0", tk.END)
        self.txt.insert("1.0", "等待语音输入…", "title")
        self.txt.insert("end", "\n\n按住  ")
        self.txt.insert("end", "Right Ctrl", "kbd")
        self.txt.insert("end", "  说话，或点击上方按钮")
        self.txt.insert("end", "\n切换  ")
        self.txt.insert("end", "Ctrl + Shift + F9", "kbd")
        self.txt.configure(state=tk.DISABLED)

    # ─────────────── 录音按钮交互 ───────────────
    def _on_press(self):
        if not self._rec_btn_pressed and state["enabled"]:
            self._rec_btn_pressed = True
            self._set_rec_visual(True)
            self._trigger_press_anim()
            start_recording()

    def _on_release(self):
        if self._rec_btn_pressed:
            self._rec_btn_pressed = False
            self._set_rec_visual(False)
            stop_recording()

    def _root_release(self, event):
        if self._rec_btn_pressed and event.widget is not self.rbtn_cv:
            self._rec_btn_pressed = False
            self._set_rec_visual(False)
            stop_recording()

    def _trigger_press_anim(self):
        """按压时按钮 scale 弹动: 0.95 → 1.08 → 1.0, 持续 ~280ms"""
        self._press_anim = [(0, 0.95), (40, 1.08), (140, 1.0), (220, 1.04), (280, 1.0)]
        self._press_anim_t = 0

    def _set_rec_visual(self, on):
        """切换录音态视觉"""
        c = self.c
        if on:
            self.rbtn_cv.itemconfigure(self.rbtn_circle, fill=c["err"])
            self.rbtn_cv.itemconfigure(self.rbtn_icon, text="■", fill="#ffffff",
                                       font=("Segoe UI Symbol", 22))
            for p in self._pulses: self.rbtn_cv.itemconfigure(p, state="normal")
            self.rbtn_cv.itemconfigure(self._breath, state="normal")
            for b in self._sp_bars: self.sp_cv.itemconfigure(b, state="normal")
            self.rbtn_hint.configure(text="松开结束", fg=c["err"])
            self.key_pill.configure(text=" 释放 Ctrl ", fg=c["err"], bg=c["card"])
        else:
            self.rbtn_cv.itemconfigure(self.rbtn_circle, fill=c["ok"])
            self.rbtn_cv.itemconfigure(self.rbtn_icon, text="🎙", fill=c["bg"],
                                       font=("Segoe UI Emoji", 24))
            for p in self._pulses: self.rbtn_cv.itemconfigure(p, state="hidden")
            self.rbtn_cv.itemconfigure(self._breath, state="hidden")
            for b in self._sp_bars: self.sp_cv.itemconfigure(b, state="hidden")
            self.rbtn_hint.configure(text="按住说话", fg=c["fg2"])
            self.key_pill.configure(text=" Right Ctrl ", fg=c["accent"], bg=c["card"])
            self._press_anim = None

    def _animate(self):
        """40ms 循环动画: 3 圈脉冲 / 呼吸光 / 频谱条 / 状态点 / 按压弹动"""
        c = self.c
        t = time.time()

        # 按压弹动 (scale 修改主体圆半径)
        if self._press_anim:
            self._press_anim_t += 40
            scale = 1.0
            for t_off, s in self._press_anim:
                if self._press_anim_t >= t_off:
                    scale = s
            if self._press_anim_t >= self._press_anim[-1][0]:
                self._press_anim = None
            self._apply_btn_scale(scale)

        if state["recording"]:
            # 3 圈脉冲环: 每圈错相 0.2s, 半径 0→基础半径 (从中心向外扩散)
            for i, base_r in enumerate(self._RB_PULSE):
                phase = (t * 1.4 + i * 0.35) % 1.0  # 0..1 循环
                r = base_r * (0.6 + 0.4 * phase)
                alpha_factor = 1.0 - phase  # 越外越淡
                width = max(1, int(3 * alpha_factor))
                self.rbtn_cv.coords(self._pulses[i],
                                    self._RB_CX - r, self._RB_CY - r,
                                    self._RB_CX + r, self._RB_CY + r)
                self.rbtn_cv.itemconfigure(self._pulses[i], width=width)
            # 呼吸光外圈
            br = self._RB_BREATH + 2 * abs(math.sin(t * 2.5))
            self.rbtn_cv.coords(self._breath,
                                self._RB_CX - br, self._RB_CY - br,
                                self._RB_CX + br, self._RB_CY + br)
            # 频谱条
            history = [0.0] * self._SP_BARS
            if _current_recorder is not None:
                try: history = _current_recorder.history
                except Exception: pass
            self._draw_spectrum(history)
            # 状态点呼吸
            r2 = 5 + 1.2 * abs(math.sin(t * 2))
            self.dot_cv.coords(self.dot_id, 7 - r2, 7 - r2, 7 + r2, 7 + r2)
        else:
            # 频谱条归零 (从外到内逐根收缩)
            for i, bar in enumerate(self._sp_bars):
                x0, _, x1, _ = self.sp_cv.coords(bar)
                self.sp_cv.coords(bar, x0, 18, x1, 18)
            self.dot_cv.coords(self.dot_id, 2, 2, 12, 12)
        self.root.after(40, self._animate)

    def _apply_btn_scale(self, scale):
        r = self._RB_R * scale
        self.rbtn_cv.coords(self.rbtn_circle,
                            self._RB_CX - r, self._RB_CY - r,
                            self._RB_CX + r, self._RB_CY + r)
        # emoji 同步缩放 (用 font size)
        font_size = max(14, int(24 * scale))
        self.rbtn_cv.itemconfigure(self.rbtn_icon, font=("Segoe UI Emoji", font_size))

    def _draw_spectrum(self, history):
        """绘制 32 根频谱条, 中心高两侧低 (sin 包络)"""
        c = self.c
        # 取最近 32 帧, 不足补 0
        h = list(history)[-self._SP_BARS:]
        if len(h) < self._SP_BARS:
            h = [0.0] * (self._SP_BARS - len(h)) + h
        cy = 18  # 频谱条中心 Y
        for i, lvl in enumerate(h):
            # 中心高两侧低 (sin 包络, 中心 1.0, 边缘 0)
            env = math.sin((i + 0.5) / self._SP_BARS * math.pi)
            lvl_s = self._level_smooth * 0.5 + lvl * 0.5  # 平滑
            bar_h = max(2, lvl_s * self._SP_MAX_H * env * 1.6)  # 放大灵敏度
            y0 = cy - bar_h / 2
            y1 = cy + bar_h / 2
            # 颜色: 高度 > 20 红色, > 12 黄色, 否则绿色
            if bar_h > 20: col = c["err"]
            elif bar_h > 12: col = c["warn"]
            else: col = c["ok"]
            self.sp_cv.coords(self._sp_bars[i],
                              self.sp_cv.coords(self._sp_bars[i])[0], y0,
                              self.sp_cv.coords(self._sp_bars[i])[2], y1)
            self.sp_cv.itemconfigure(self._sp_bars[i], fill=col)

    def _toggle(self):
        state["enabled"] = not state["enabled"]
        self._refresh()
        if not state["enabled"] and state["recording"]:
            stop_recording()
            self._on_release()

    def _refresh(self):
        c = self.c
        # 模式徽章
        mode = state.get("audio_mode", "共享")
        guarded = state.get("mic_guarded", False)
        mtext = ""
        if "独占" in mode: mtext += " ⓦ独占"
        if guarded: mtext += " ⓜ麦克风独占"
        self.mode_lbl.configure(text=mtext)
        # 状态
        if state["recording"]:
            self._set_rec_visual(True)
            self.lbl.configure(text="录音中", fg=c["err"])
            self.dot_cv.itemconfigure(self.dot_id, fill=c["err"])
            self.tgl.configure(text="  停止  ", bg=c["err"], fg="#ffffff",
                               activebackground=c["err"])
            self.eng_badge.configure(text="", bg=c["bg"])
        elif state["enabled"]:
            eng = state["engine"]
            if eng == "本地":
                self.lbl.configure(text="已启用  ·  本地", fg=c["fg"])
                self.dot_cv.itemconfigure(self.dot_id, fill=c["accent2"])
                self.eng_badge.configure(text="  本地  ", fg=c["accent2"], bg=c["card"])
            else:
                self.lbl.configure(text="待命", fg=c["fg"])
                self.dot_cv.itemconfigure(self.dot_id, fill=c["ok"])
                self.eng_badge.configure(text="", bg=c["bg"])
            self._set_rec_visual(False)
            self.tgl.configure(text="  禁用  ", bg=c["card"], fg=c["fg2"],
                               activebackground=c["card2"])
        else:
            self.lbl.configure(text="已禁用", fg=c["fg3"])
            self.dot_cv.itemconfigure(self.dot_id, fill=c["fg3"])
            self.eng_badge.configure(text="", bg=c["bg"])
            self._set_rec_visual(False)
            self.tgl.configure(text="  启用  ", bg=c["ok"], fg=c["bg"],
                               activebackground="#b4e08a")

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
                    self.txt.insert("1.0", "❌  " + m[1], "error")
                    self.txt.configure(state=tk.DISABLED)
                elif k == "toggled": self._refresh(); self._update_tray()
                elif k == "show": self.root.deiconify(); self.root.lift()
                elif k == "status": self._refresh()
        except queue_mod.Empty:
            pass
        self.root.after(100, self._poll)

    def _copy_result(self):
        try:
            content = self.txt.get("1.0", tk.END).strip()
            if content and content != "等待语音输入…":
                pyperclip.copy(content)
                # 短暂反馈
                old = self.copy_btn.cget("text")
                self.copy_btn.configure(text="✓  已复制", fg=self.c["ok"])
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
        messagebox.showinfo("关于",
            "言栖 v5.0\n"
            "Voice Input for AI Agents\n\n"
            "作者: 孙欣阳 (Xinyang Sun)\n\n"
            "本地离线识别 (sherpa-onnx + SenseVoice 多语种)\n"
            "  → 支持中文 / 英文 / 日文 / 韩文 / 粤语 自动检测\n"
            "  → 离线运行, 无网亦可用, 无配额限制\n\n"
            "录音时: WASAPI 独占流 + 切换系统默认麦克风\n"
            "  → 其他 app (Discord/QQ/飞书) 拿不到语音\n"
            "  → 松开 Ctrl 立即恢复\n\n"
            "按住 Right Ctrl / 点击录音按钮\n"
            "Ctrl+Shift+F9 开关\n"
            "设置中可调整: 开机启动 / 独占设备 / 麦克风 / 识别语言\n\n"
            "项目主页: https://github.com/Xinyang-S/STT-YanQi",
            parent=self.root)


# ═══════════════════════════════════════════════════════════
#  设置窗口 (API + 音频设备)
# ═══════════════════════════════════════════════════════════
class SettingsDialog:
    def __init__(self, parent, main_win):
        self.mw = main_win
        c = main_win.c
        self.win = tk.Toplevel(parent)
        self.win.title("设置 — 言栖")
        self.win.geometry("480x500")
        self.win.resizable(False, False)
        self.win.configure(bg=c["bg"])
        self.win.transient(parent)
        self.win.grab_set()
        # 定位在主窗口右侧
        px, py = parent.winfo_x(), parent.winfo_y()
        pw = parent.winfo_width()
        self.win.geometry(f"+{px + pw + 8}+{py}")
        # 自定义 Notebook 样式 (深色)
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TNotebook", background=c["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=c["bg2"], foreground=c["fg2"],
                        padding=(16, 8), font=("Microsoft YaHei UI", 9), borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", c["card"]), ("active", c["card2"])],
                  foreground=[("selected", c["accent"]), ("active", c["fg"])])
        style.configure("TFrame", background=c["bg"])
        nb = ttk.Notebook(self.win)
        nb.pack(fill=tk.BOTH, expand=True, padx=14, pady=(14, 8))
        nb.add(self._general_tab(nb), text="  通用  ")
        nb.add(self._audio_tab(nb), text="  音频设备  ")
        # 底栏提示
        tk.Label(self.win, text="设置修改后立即生效",
                 font=("Microsoft YaHei UI", 8), fg=c["fg3"], bg=c["bg"]).pack(pady=(0, 12))

    def _general_tab(self, p):
        c = self.mw.c
        f = tk.Frame(p, bg=c["bg"], padx=4, pady=4)
        # 开机启动
        self._section_label(f, "开机启动", "accent")
        card = tk.Frame(f, bg=c["card"], highlightthickness=1, highlightbackground=c["border"])
        card.pack(fill=tk.X, pady=(4, 0))
        self.auto_start_var = tk.BooleanVar(value=config.get("auto_start", True))
        cb = tk.Checkbutton(
            card, text="开机时自动启动 (登录后直接进托盘)",
            variable=self.auto_start_var,
            font=("Microsoft YaHei UI", 10), fg=c["fg"], bg=c["card"],
            selectcolor=c["card2"], activebackground=c["card2"], activeforeground=c["fg"],
            bd=0, highlightthickness=0, cursor="hand2", padx=10, pady=8,
            command=self._auto_start_toggle)
        cb.pack(anchor=tk.W)
        if not getattr(sys, "frozen", False):
            tk.Label(f, text="⚠ 当前为开发模式, 注册表项不会写入 (打包后才生效)",
                     font=("Microsoft YaHei UI", 8), fg=c["warn"], bg=c["bg"],
                     justify=tk.LEFT).pack(anchor=tk.W, pady=(10, 0))
        else:
            status = "已注册到 HKCU\\...\\Run" if is_auto_start_enabled() else "未注册"
            tk.Label(f, text=f"当前状态: {status}",
                     font=("Microsoft YaHei UI", 8), fg=c["fg3"], bg=c["bg"],
                     justify=tk.LEFT).pack(anchor=tk.W, pady=(10, 0))

        # 录音时独占设备
        self._section_label(f, "录音隐私", "accent2")
        card2 = tk.Frame(f, bg=c["card"], highlightthickness=1, highlightbackground=c["border"])
        card2.pack(fill=tk.X, pady=(4, 0))
        self.exclusive_var = tk.BooleanVar(value=config.get("exclusive_device", True))
        cb2 = tk.Checkbutton(
            card2, text="录音时独占设备 (推荐开启)",
            variable=self.exclusive_var,
            font=("Microsoft YaHei UI", 10), fg=c["fg"], bg=c["card"],
            selectcolor=c["card2"], activebackground=c["card2"], activeforeground=c["fg"],
            bd=0, highlightthickness=0, cursor="hand2", padx=10, pady=4,
            command=self._exclusive_toggle)
        cb2.pack(anchor=tk.W)
        tk.Label(card2,
                 text=("  开启: 录音时切换系统默认麦克风, 阻止其他 App (Discord / QQ / 飞书)\n"
                       "        获取您的声音; 同时尝试 WASAPI 独占流。\n"
                       "  关闭: 与其他 App 共享麦克风, 互不干扰 (适合多人协作或会议场景)。"),
                 font=("Microsoft YaHei UI", 8), fg=c["fg3"], bg=c["card"],
                 justify=tk.LEFT, padx=10, pady=(0, 8)).pack(anchor=tk.W)
        return f

    def _auto_start_toggle(self):
        """勾选/取消开机启动, 立即写注册表 + 持久化到 config"""
        enabled = self.auto_start_var.get()
        config["auto_start"] = enabled
        set_auto_start(enabled)
        save_config()
        log(f"用户切换开机启动: {enabled}")

    def _exclusive_toggle(self):
        """勾选/取消独占设备, 立即持久化"""
        enabled = self.exclusive_var.get()
        config["exclusive_device"] = enabled
        save_config()
        log(f"用户切换独占设备: {enabled}")

    def _mk_entry_row(self, parent, label, key_dict, key_name, show="", accent="accent"):
        """一行: 标签 + 输入框, 返回 Entry"""
        c = self.mw.c
        row = tk.Frame(parent, bg=c["bg"])
        row.pack(fill=tk.X, pady=4)
        tk.Label(row, text=label, font=("Microsoft YaHei UI", 9), fg=c["fg2"],
                 bg=c["bg"], width=12, anchor=tk.W).pack(side=tk.LEFT)
        container = tk.Frame(row, bg=c["card"], highlightthickness=1,
                             highlightbackground=c["border"])
        container.pack(side=tk.LEFT, fill=tk.X, expand=True)
        e = tk.Entry(container, font=("Consolas", 9), show=show, relief=tk.FLAT,
                     bg=c["card"], fg=c["fg"], insertbackground=c[accent],
                     bd=0, highlightthickness=0)
        e.insert(0, key_dict.get(key_name, ""))
        e.pack(fill=tk.X, padx=8, pady=6)
        return e

    def _section_label(self, parent, text, color_key="accent"):
        """分组小标题"""
        c = self.mw.c
        f = tk.Frame(parent, bg=c["bg"])
        f.pack(fill=tk.X, pady=(10, 4))
        tk.Label(f, text="●", font=("Arial", 10), fg=c[color_key], bg=c["bg"]).pack(side=tk.LEFT, padx=(0, 6))
        tk.Label(f, text=text, font=("Microsoft YaHei UI", 10, "bold"),
                 fg=c["fg"], bg=c["bg"]).pack(side=tk.LEFT)

    def _audio_tab(self, p):
        c = self.mw.c
        f = tk.Frame(p, bg=c["bg"], padx=4, pady=4)
        self._section_label(f, "选择麦克风  (即时生效)", "accent")
        devs = AudioRecorder.list_devices()
        self.dv = tk.StringVar(); self.dm = {}
        for idx, name, is_def in devs:
            lb = f"{name}  {'(默认)' if is_def else ''}"
            self.dm[lb] = idx
            if idx == config.get("input_device_index"): self.dv.set(lb)
            elif is_def and config.get("input_device_index") is None: self.dv.set(lb)
        # 卡片化设备列表
        if devs:
            list_frame = tk.Frame(f, bg=c["card"], highlightthickness=1,
                                  highlightbackground=c["border"])
            list_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
            for lb in self.dm:
                row = tk.Frame(list_frame, bg=c["card"])
                row.pack(fill=tk.X, padx=2, pady=2)
                rb = tk.Radiobutton(row, text="  " + lb, variable=self.dv, value=lb,
                                    fg=c["fg"], bg=c["card"], selectcolor=c["card2"],
                                    activebackground=c["card2"], activeforeground=c["fg"],
                                    font=("Microsoft YaHei UI", 9), anchor=tk.W,
                                    cursor="hand2", bd=0, highlightthickness=0,
                                    command=self._dev_save)
                rb.pack(fill=tk.X, padx=4, pady=4)
            hint_text = "录音时"
            if config.get("exclusive_device", True):
                hint_text += "自动尝试 WASAPI 独占 + 切换系统默认麦克风\n按住 Right Ctrl 期间, 其他 app 听不到您的声音"
            else:
                hint_text += "使用共享模式, 其他 app 也能正常获取音频"
            hint = tk.Label(f, text=hint_text,
                            font=("Microsoft YaHei UI", 8), fg=c["fg3"], bg=c["bg"],
                            justify=tk.LEFT)
            hint.pack(anchor=tk.W, pady=(10, 0))
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
    log("退出"); icon.stop(); os._exit(0)

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
    print("  言栖 v5.0 - 全链路测试")
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

    log(f"言栖 v5.0 启动 (minimized={minimized})")
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
            "言栖 v5.0"
        )).start()

    win.root.mainloop()
    stopped.set(); kb.stop(); state["enabled"] = False; log("程序退出")


if __name__ == "__main__":
    main()
