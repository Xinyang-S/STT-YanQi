#!/usr/bin/env python3
"""Core runtime for YanQi voice input.

This module intentionally contains no Tkinter, pystray, or legacy desktop UI
imports. It is used by the Tauri sidecar and can also be shared by legacy
shells that need the recording/ASR backend.
"""

import ctypes
import json
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
import queue as queue_mod

try:
    import pyaudio
except ImportError as exc:
    raise RuntimeError("缺少依赖: pyaudio") from exc
try:
    import numpy as np
except ImportError as exc:
    raise RuntimeError("缺少依赖: numpy") from exc
try:
    import pyperclip
except ImportError as exc:
    raise RuntimeError("缺少依赖: pyperclip") from exc
try:
    from comtypes import GUID
except ImportError as exc:
    raise RuntimeError("缺少依赖: comtypes") from exc

try:
    from sherpa_onnx import OfflineRecognizer
    _HAS_SHERPA = True
except ImportError:
    OfflineRecognizer = None
    _HAS_SHERPA = False

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
_WAV_TOGGLE_OFF = _gen_tone([(1047, 0.05), (740, 0.06), (523, 0.06)])  # 三音: 禁用 = 更清晰的下行
_WAV_ERROR     = _gen_tone([(262, 0.15)])                 # 低沉

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
                              ("error", _WAV_ERROR)]:
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
        """立即同步播放某个声音, 用于按下录音和启停这类短事件提示音."""
        if not _SOUND_READY: _init_sounds()
        try:
            path = _SOUND_FILE_PATHS.get(name)
            if path and os.path.isfile(path):
                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_NODEFAULT)
        except Exception as e:
            log(f"音效[{name}]同步播放失败: {e!r}")
except ImportError:
    def _play(name):  return
    def _play_sync(name):  return

# v0.5.0: 音效统一为命名 + 后台串行 worker 模式. 修复按下快捷键时无提示音:
# 之前 sound_start 用 SND_ASYNC + 200ms 临时文件, 在 Windows 实际开始读文件
# 之前就被 unlink → 启动音被截断. 现在用持久文件 + 串行 worker, 100% 可闻.
def sound_start():       _play_sync("start")
def sound_done():        _play("done")
def sound_toggle_on():   _play_sync("toggle_on")
def sound_toggle_off():  _play_sync("toggle_off")
def sound_error():       _play("error")


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
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            try:
                saved = json.load(f)
            except json.JSONDecodeError:
                saved = {}
        next_config = {**DEFAULT_CONFIG, **saved}
        # 清理旧版字段 (baidu / iflytek / local_asr)
        for k in ("baidu", "iflytek", "local_asr"):
            next_config.pop(k, None)
        # 补全新字段
        for k, v in DEFAULT_CONFIG.items():
            next_config.setdefault(k, v)
    else:
        next_config = DEFAULT_CONFIG.copy()
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(next_config, f, indent=2, ensure_ascii=False)

    config.clear()
    config.update(next_config)

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
                log(f"音频采样率 {sr} != 16000, 自动重采样到 16000")
                samples = _resample_to_16k(samples, sr)
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
            if sr != 16000:
                log(f"音频采样率 {sr} != 16000, 自动重采样到 16000")
                samples = _resample_to_16k(samples, sr)
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


def _resample_to_16k(samples, source_rate):
    if source_rate == 16000 or len(samples) == 0:
        return samples
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    duration = len(samples) / float(source_rate)
    target_len = max(1, int(duration * 16000))
    src_x = np.linspace(0.0, duration, num=len(samples), endpoint=False)
    dst_x = np.linspace(0.0, duration, num=target_len, endpoint=False)
    return np.interp(dst_x, src_x, samples).astype(np.float32)


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
        self.sample_rate = config["sample_rate"]

    def _candidate_sample_rates(self, device_index):
        preferred = int(config.get("sample_rate", 16000))
        candidates = [preferred]
        try:
            if device_index is not None:
                info = self.p.get_device_info_by_index(device_index)
            else:
                info = self.p.get_default_input_device_info()
            default_rate = int(float(info.get("defaultSampleRate") or preferred))
            candidates.append(default_rate)
        except Exception as e:
            log(f"读取设备默认采样率失败: {e!r}")
        candidates.extend([48000, 44100, 32000, 16000])

        seen = set()
        result = []
        for rate in candidates:
            try:
                rate = int(rate)
            except Exception:
                continue
            if rate > 0 and rate not in seen:
                seen.add(rate)
                result.append(rate)
        return result

    def start(self):
        self.frames = []; self._active = True
        idx = config.get("input_device_index")
        cb = self._cb
        ch = config["channels"]; cs = config["chunk_size"]
        sample_rates = self._candidate_sample_rates(idx)
        last_error = None

        # 1) 独占开关: 用户在设置中关闭 → 直接走共享模式 (不打扰其他 app)
        # 2) 独占开启 → 先尝试 WASAPI 独占, 失败回退共享
        if config.get("exclusive_device", True):
            for sr in sample_rates:
                self.stream, self._mode = try_open_exclusive_stream(
                    self.p, cb, sr, ch, cs, idx)
                if self.stream is not None:
                    self.sample_rate = sr
                    break
        else:
            self.stream, self._mode = None, "共享"

        if self.stream is None:
            for sr in sample_rates:
                kw = dict(format=pyaudio.paInt16, channels=ch, rate=sr,
                          input=True, frames_per_buffer=cs, stream_callback=cb)
                if idx is not None: kw["input_device_index"] = idx
                try:
                    self.stream = self.p.open(**kw)
                    self._mode = "共享"
                    self.sample_rate = sr
                    break
                except Exception as e:
                    last_error = e
                    log(f"共享模式采样率 {sr} 打开失败: {e!r}")
            if self.stream is None and last_error is not None:
                raise last_error

        state["audio_mode"] = self._mode
        log(f"音频: {self._mode}模式, sample_rate={self.sample_rate}")

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
            wf.setframerate(self.sample_rate); wf.writeframes(b"".join(frames))

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
        user32 = ctypes.windll.user32
        key_up = 0x0002
        vk_ctrl = 0x11
        vk_v = 0x56
        try:
            user32.keybd_event(vk_ctrl, 0, 0, 0)
            time.sleep(0.02)
            user32.keybd_event(vk_v, 0, 0, 0)
            time.sleep(0.02)
            user32.keybd_event(vk_v, 0, key_up, 0)
            time.sleep(0.02)
        finally:
            user32.keybd_event(vk_ctrl, 0, key_up, 0)
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
    sound_start()
    threading.Thread(target=_recording_wrapper, daemon=True).start()


def _recording_wrapper():
    try: recording_flow()
    finally: _recording_lock.release()


def stop_recording():
    state["recording"] = False
