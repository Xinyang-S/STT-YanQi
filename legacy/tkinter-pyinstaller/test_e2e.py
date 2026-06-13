"""端到端自测脚本 — 模拟完整录音 + 独占 + 识别流程"""
import sys, os, time, traceback
sys.path.insert(0, os.path.dirname(__file__))
import voice_input as vi

def test():
    print("=" * 55)
    print("  言栖 v0.5.0 — 自动化端到端测试")
    print("=" * 55)

    # 1. 配置
    print("\n[1] 加载配置 + 引擎检查...")
    hl = vi.load_config()
    print(f"  sherpa-onnx:  {'OK' if vi._HAS_SHERPA else '未安装'}")
    d, t, m = vi._resolve_model_dir()
    print(f"  SenseVoice:   {'OK' if d else '缺失'}")
    if not hl:
        print("  FAIL: 本地引擎不可用")
        return False

    # 2. 设备
    print("\n[2] 音频设备...")
    devs = vi.AudioRecorder.list_devices()
    print(f"  共 {len(devs)} 个录音设备")
    default_dev = None
    for idx, name, is_def in devs:
        if is_def: default_dev = idx; break
    print(f"  默认设备索引: {default_dev}")

    # 3. MicGuard 测试
    print("\n[3] MicGuard 切换测试...")
    g = vi.MicGuard()
    g.__enter__()
    fallback_ok = g._fallback_id is not None
    print(f"  备用设备: {'OK' if fallback_ok else 'FAIL'}")
    g.__exit__(None, None, None)
    print("  恢复: OK")
    if not fallback_ok:
        print("  FAIL: 无备用设备")
        return False

    # 4. 模拟完整录音流程
    print("\n[4] 录音+MicGuard 联调测试 (请对着麦克风说话)...")
    vi.state['enabled'] = True
    vi.state['recording'] = True

    rec = vi.AudioRecorder()
    g2 = vi.MicGuard()
    g2.__enter__()

    rec.start()
    print(f"  录音流已打开 (模式={rec.mode})")

    for i in range(3, 0, -1):
        print(f"  ...{i}s")
        time.sleep(1)

    frames = rec.stop()
    print(f"  录音结束: {len(frames)} 帧")

    rec.close()
    g2.__exit__(None, None, None)
    print("  MicGuard 已恢复")

    if len(frames) < 10:
        print("  FAIL: 录音数据不足 (<10帧)")
        return False

    # 5. 保存音频
    import tempfile, wave
    path = os.path.join(str(vi.CONFIG_DIR), "e2e_test.wav")
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"".join(frames))
    print(f"\n[5] 音频已保存: {path}")

    # 6. 识别
    print("\n[6] 本地识别...")
    asr = vi.ASRManager()
    try:
        text, engine = asr.transcribe(path)
        print(f"  结果 [{engine}]: {text}")
        if text and len(text.strip()) > 0:
            print("\n" + "=" * 55)
            print(f"  ✓ 测试通过! 识别结果: {text}")
            print("=" * 55)
            return True
        else:
            print("  FAIL: 识别结果为空")
            return False
    except Exception as e:
        print(f"  FAIL: {e}")
        traceback.print_exc()
        return False

if __name__ == "__main__":
    ok = test()
    sys.exit(0 if ok else 1)
