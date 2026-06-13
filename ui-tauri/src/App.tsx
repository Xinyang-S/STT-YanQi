import { type CSSProperties, type MouseEvent, type PointerEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import {
  AudioLines,
  Check,
  Copy,
  Keyboard,
  Minus,
  Mic,
  Mic2,
  MousePointer2,
  Palette,
  Pause,
  Power,
  RotateCcw,
  Settings,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  X,
} from "lucide-react";
import "./App.css";

const API = "http://127.0.0.1:47632";

type BackendState = {
  service: string;
  last_event: string;
  last_event_at: number;
  enabled: boolean;
  recording: boolean;
  engine: string;
  last_text: string;
  last_error: string;
  audio_mode: string;
  mic_guarded: boolean;
  exclusive: boolean;
  floating_bubble: boolean;
  input_device_index: number | null;
  language: string;
};

type BackendPayload = {
  ok?: boolean;
  state?: BackendState;
};

type Device = {
  index: number;
  name: string;
  default: boolean;
};

type AppearanceMode = "liquid" | "clear";

type AppearanceConfig = {
  mode: AppearanceMode;
  accent: string;
  opacity: number;
  acrylic: boolean;
};

type ShortcutConfig = {
  keys: number[];
  mouse_buttons: number[];
  label: string;
};

const accentOptions = ["#007aff", "#00a88f", "#ff8a2a", "#ff4f8b", "#7d6bff"];

const defaultShortcut: ShortcutConfig = {
  keys: [0xa3],
  mouse_buttons: [],
  label: "Right Ctrl",
};

const genericModifierCodes = {
  ctrl: 0x11,
  shift: 0x10,
  alt: 0x12,
  meta: 0x5b,
};

function labelForKey(code: number): string {
  if (code >= 0x30 && code <= 0x39) return String.fromCharCode(code);
  if (code >= 0x41 && code <= 0x5a) return String.fromCharCode(code);
  if (code >= 0x70 && code <= 0x87) return `F${code - 0x70 + 1}`;
  const labels: Record<number, string> = {
    0x08: "Backspace",
    0x09: "Tab",
    0x0d: "Enter",
    0x10: "Shift",
    0x11: "Ctrl",
    0x12: "Alt",
    0x1b: "Esc",
    0x20: "Space",
    0x21: "Page Up",
    0x22: "Page Down",
    0x23: "End",
    0x24: "Home",
    0x25: "Left",
    0x26: "Up",
    0x27: "Right",
    0x28: "Down",
    0x2d: "Insert",
    0x2e: "Delete",
    0x5b: "Win",
    0x5c: "Right Win",
    0xa0: "Left Shift",
    0xa1: "Right Shift",
    0xa2: "Left Ctrl",
    0xa3: "Right Ctrl",
    0xa4: "Left Alt",
    0xa5: "Right Alt",
  };
  return labels[code] || `VK ${code}`;
}

function labelForMouse(button: number): string {
  const labels: Record<number, string> = {
    1: "鼠标左键",
    2: "鼠标右键",
    3: "鼠标中键",
    4: "鼠标后退键",
    5: "鼠标前进键",
  };
  return labels[button] || `鼠标键 ${button}`;
}

function keyCodeFromEvent(event: globalThis.KeyboardEvent): number {
  const byCode: Record<string, number> = {
    ControlLeft: 0xa2,
    ControlRight: 0xa3,
    ShiftLeft: 0xa0,
    ShiftRight: 0xa1,
    AltLeft: 0xa4,
    AltRight: 0xa5,
    MetaLeft: 0x5b,
    MetaRight: 0x5c,
    Space: 0x20,
  };
  return byCode[event.code] || event.keyCode || event.which || 0;
}

function modifierKeysFromEvent(event: globalThis.KeyboardEvent | globalThis.MouseEvent, mainCode?: number): number[] {
  const keys: number[] = [];
  if (event.ctrlKey && mainCode !== 0xa2 && mainCode !== 0xa3) keys.push(genericModifierCodes.ctrl);
  if (event.shiftKey && mainCode !== 0xa0 && mainCode !== 0xa1) keys.push(genericModifierCodes.shift);
  if (event.altKey && mainCode !== 0xa4 && mainCode !== 0xa5) keys.push(genericModifierCodes.alt);
  if (event.metaKey && mainCode !== 0x5b && mainCode !== 0x5c) keys.push(genericModifierCodes.meta);
  return keys;
}

function normalizeShortcut(keys: number[], mouseButtons: number[]): ShortcutConfig | null {
  const uniqueKeys = Array.from(new Set(keys.filter((code) => code > 0 && code <= 255)));
  const uniqueMouseButtons = Array.from(new Set(mouseButtons.filter((button) => button >= 1 && button <= 5)));
  if (!uniqueKeys.length && !uniqueMouseButtons.length) return null;
  const labelParts = [...uniqueKeys.map(labelForKey), ...uniqueMouseButtons.map(labelForMouse)];
  return {
    keys: uniqueKeys,
    mouse_buttons: uniqueMouseButtons.slice(0, 1),
    label: labelParts.join(" + "),
  };
}

function isModifierCode(code: number): boolean {
  return [0x10, 0x11, 0x12, 0x5b, 0x5c, 0xa0, 0xa1, 0xa2, 0xa3, 0xa4, 0xa5].includes(code);
}

function mouseButtonFromEvent(event: globalThis.MouseEvent): number | null {
  const buttons: Record<number, number> = {
    0: 1,
    1: 3,
    2: 2,
    3: 4,
    4: 5,
  };
  return buttons[event.button] || null;
}

const fallbackState: BackendState = {
  service: "connecting",
  last_event: "",
  last_event_at: 0,
  enabled: true,
  recording: false,
  engine: "none",
  last_text: "",
  last_error: "",
  audio_mode: "共享",
  mic_guarded: false,
  exclusive: true,
  floating_bubble: false,
  input_device_index: null,
  language: "auto",
};

const defaultAppearance: AppearanceConfig = {
  mode: "liquid",
  accent: "#007aff",
  opacity: 58,
  acrylic: true,
};

function loadAppearance(): AppearanceConfig {
  try {
    const saved = JSON.parse(window.localStorage.getItem("yanqi-appearance") || "null") as Partial<AppearanceConfig> | null;
    return {
      ...defaultAppearance,
      ...saved,
      mode: saved?.mode === "clear" ? "clear" : "liquid",
      accent: accentOptions.includes(saved?.accent || "") ? saved?.accent || defaultAppearance.accent : defaultAppearance.accent,
      opacity: Math.min(100, Math.max(0, Number(saved?.opacity ?? defaultAppearance.opacity))),
      acrylic: saved?.acrylic !== false,
    };
  } catch {
    return defaultAppearance;
  }
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    headers: { "content-type": "application/json" },
    ...init,
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json() as Promise<T>;
}

function App() {
  const isBubbleView = new URLSearchParams(window.location.search).get("view") === "bubble";
  const [state, setState] = useState<BackendState>(fallbackState);
  const [devices, setDevices] = useState<Device[]>([]);
  const [backendRunning, setBackendRunning] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [appearanceOpen, setAppearanceOpen] = useState(false);
  const [appearance, setAppearance] = useState<AppearanceConfig>(loadAppearance);
  const [shortcut, setShortcut] = useState<ShortcutConfig>(defaultShortcut);
  const [capturingShortcut, setCapturingShortcut] = useState(false);
  const [copied, setCopied] = useState(false);
  const pointerRecording = useRef(false);
  const shortcutModifierKeys = useRef<number[]>([]);

  const refresh = useCallback(async () => {
    try {
      const info = await invoke<{ running: boolean }>("backend_info");
      setBackendRunning(Boolean(info.running));
      const payload = await api<{ ok: boolean; state: BackendState }>("/api/status");
      setState(payload.state);
    } catch {
      setBackendRunning(false);
      setState((prev) => ({ ...prev, service: "offline" }));
    }
  }, []);

  const refreshDevices = useCallback(async () => {
    try {
      const payload = await api<{ ok: boolean; devices: Device[] }>("/api/devices");
      setDevices(payload.devices);
    } catch {
      setDevices([]);
    }
  }, []);

  useEffect(() => {
    refresh();
    refreshDevices();
    void invoke<ShortcutConfig>("get_shortcut_config")
      .then(setShortcut)
      .catch(() => setShortcut(defaultShortcut));
  }, [refresh, refreshDevices]);

  const applyShortcut = useCallback(async (next: ShortcutConfig) => {
    const saved = await invoke<ShortcutConfig>("set_shortcut_config", { config: next });
    setShortcut(saved);
    setCapturingShortcut(false);
  }, []);

  useEffect(() => {
    if (!capturingShortcut) return undefined;
    shortcutModifierKeys.current = [];

    const captureKeyboard = (event: globalThis.KeyboardEvent) => {
      event.preventDefault();
      event.stopPropagation();

      if (event.key === "Escape") {
        setCapturingShortcut(false);
        return;
      }

      const mainCode = keyCodeFromEvent(event);
      if (isModifierCode(mainCode)) {
        shortcutModifierKeys.current = Array.from(new Set([...shortcutModifierKeys.current, mainCode]));
        return;
      }

      const next = normalizeShortcut([...modifierKeysFromEvent(event, mainCode), mainCode], []);
      if (next) void applyShortcut(next);
    };

    const captureKeyboardRelease = (event: globalThis.KeyboardEvent) => {
      if (!shortcutModifierKeys.current.length) return;
      event.preventDefault();
      event.stopPropagation();

      const next = normalizeShortcut(shortcutModifierKeys.current, []);
      if (next) void applyShortcut(next);
    };

    const captureMouse = (event: globalThis.MouseEvent) => {
      if ((event.target as Element | null)?.closest("[data-shortcut-control='true']")) return;
      event.preventDefault();
      event.stopPropagation();

      const button = mouseButtonFromEvent(event);
      if (!button) return;
      const next = normalizeShortcut(modifierKeysFromEvent(event), [button]);
      if (next) void applyShortcut(next);
    };

    const blockContextMenu = (event: globalThis.MouseEvent) => {
      if ((event.target as Element | null)?.closest("[data-shortcut-control='true']")) return;
      event.preventDefault();
      event.stopPropagation();
    };

    window.addEventListener("keydown", captureKeyboard, true);
    window.addEventListener("keyup", captureKeyboardRelease, true);
    window.addEventListener("mousedown", captureMouse, true);
    window.addEventListener("contextmenu", blockContextMenu, true);
    return () => {
      shortcutModifierKeys.current = [];
      window.removeEventListener("keydown", captureKeyboard, true);
      window.removeEventListener("keyup", captureKeyboardRelease, true);
      window.removeEventListener("mousedown", captureMouse, true);
      window.removeEventListener("contextmenu", blockContextMenu, true);
    };
  }, [applyShortcut, capturingShortcut]);

  useEffect(() => {
    if (!settingsOpen) setCapturingShortcut(false);
  }, [settingsOpen]);

  const serviceLabel = useMemo(() => {
    if (!backendRunning || state.service === "offline") return "后端未连接";
    if (state.service === "engine_missing") return "模型未就绪";
    if (state.recording) return "正在录音";
    if (!state.enabled) return "已暂停";
    return "待命";
  }, [backendRunning, state]);

  const resultText =
    state.last_text ||
    (state.last_error ? `错误: ${state.last_error}` : `按住 ${shortcut.label} 或点击中央液态麦克风开始。`);

  const shellStyle = useMemo(() => {
    const alpha = appearance.opacity / 100;
    return {
        "--accent": appearance.accent,
        "--panel-alpha": alpha.toFixed(2),
        "--window-alpha": (0.07 + alpha * 0.83).toFixed(2),
        "--glass-alpha": (0.05 + alpha * 0.82).toFixed(2),
        "--glass-soft-alpha": (0.03 + alpha * 0.36).toFixed(2),
        "--chrome-alpha": (0.06 + alpha * 0.52).toFixed(2),
        "--line-alpha": (0.22 + alpha * 0.48).toFixed(2),
        "--aura-alpha": (0.16 + alpha * 0.46).toFixed(2),
        "--glass-blur": appearance.acrylic ? `${14 + alpha * 18}px` : "0px",
        "--glass-saturation": appearance.acrylic ? `${1.08 + alpha * 0.78}` : "1",
      } as CSSProperties;
  }, [appearance]);

  useEffect(() => {
    window.localStorage.setItem("yanqi-appearance", JSON.stringify(appearance));
  }, [appearance]);

  const post = useCallback(
    async (path: string, body?: unknown) => {
      try {
        const payload = await api<BackendPayload>(path, {
          method: "POST",
          body: body ? JSON.stringify(body) : "{}",
        });
        if (payload.state) setState(payload.state);
      } catch {
        refresh();
      }
    },
    [refresh],
  );

  const applyBackendPayload = useCallback((payload: BackendPayload) => {
    if (payload.state) setState(payload.state);
  }, []);

  async function invokeBackendCommand(name: "start_recording" | "stop_recording" | "toggle_enabled") {
    const payload = await invoke<BackendPayload>(name);
    applyBackendPayload(payload);
    return payload;
  }

  useEffect(() => {
    let unlistenRecording: (() => void) | undefined;
    let unlistenBackend: (() => void) | undefined;

    void listen<{ recording: boolean }>("recording-changed", (event) => {
      setState((prev) => ({ ...prev, recording: event.payload.recording }));
    }).then((dispose) => {
      unlistenRecording = dispose;
    }).catch(() => {});

    void listen<BackendPayload>("backend-state", (event) => {
      setBackendRunning(true);
      applyBackendPayload(event.payload);
    }).then((dispose) => {
      unlistenBackend = dispose;
    }).catch(() => {});

    return () => {
      if (unlistenRecording) unlistenRecording();
      if (unlistenBackend) unlistenBackend();
    };
  }, [applyBackendPayload]);

  useEffect(() => {
    const stopFromWindow = () => {
      if (!pointerRecording.current) return;
      pointerRecording.current = false;
      setState((prev) => ({ ...prev, recording: false }));
      void invoke<BackendPayload>("stop_recording")
        .then(applyBackendPayload)
        .catch(() => refresh());
    };

    window.addEventListener("pointerup", stopFromWindow);
    window.addEventListener("mouseup", stopFromWindow);
    window.addEventListener("blur", stopFromWindow);
    return () => {
      window.removeEventListener("pointerup", stopFromWindow);
      window.removeEventListener("mouseup", stopFromWindow);
      window.removeEventListener("blur", stopFromWindow);
    };
  }, [applyBackendPayload, refresh]);

  async function copyResult() {
    if (!state.last_text) return;
    await navigator.clipboard.writeText(state.last_text);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1400);
  }

  function stopChromePointer(event: PointerEvent<HTMLElement>) {
    event.stopPropagation();
  }

  function stopChromeClick(event: MouseEvent<HTMLElement>) {
    event.preventDefault();
    event.stopPropagation();
  }

  async function minimizeWindow(event: MouseEvent<HTMLButtonElement>) {
    stopChromeClick(event);
    await invoke("minimize_window").catch(() => {});
  }

  async function closeWindow(event: MouseEvent<HTMLButtonElement>) {
    stopChromeClick(event);
    await invoke("close_to_tray", { showBubble: state.floating_bubble }).catch(() => refresh());
  }

  async function showMainWindow() {
    await invoke("show_main_window");
  }

  async function toggleEnabled() {
    try {
      await invokeBackendCommand("toggle_enabled");
    } catch {
      refresh();
    }
  }

  function startWindowDrag(event: PointerEvent<HTMLElement>) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    void invoke("start_window_drag");
  }

  async function startPointerRecording(event: PointerEvent<HTMLButtonElement>) {
    if (event.button !== 0 || !state.enabled || !backendRunning || pointerRecording.current) return;
    event.stopPropagation();
    pointerRecording.current = true;
    setState((prev) => ({ ...prev, recording: true }));
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // Pointer capture is best-effort; recording still works without it.
    }
    try {
      await invokeBackendCommand("start_recording");
    } catch {
      pointerRecording.current = false;
      setState((prev) => ({ ...prev, recording: false }));
      refresh();
    }
  }

  async function stopPointerRecording() {
    if (!pointerRecording.current) return;
    pointerRecording.current = false;
    setState((prev) => ({ ...prev, recording: false }));
    try {
      await invokeBackendCommand("stop_recording");
    } catch {
      refresh();
    }
  }

  function updateAppearance(next: Partial<AppearanceConfig>) {
    setAppearance((prev) => ({ ...prev, ...next }));
  }

  if (isBubbleView) {
    return (
      <main className={`bubble-shell ${state.recording ? "is-recording" : ""} ${!state.enabled ? "is-off" : ""}`}>
        {["top", "right", "bottom", "left"].map((side) => (
          <div
            key={side}
            className={`bubble-drag bubble-drag-${side}`}
            onPointerDown={startWindowDrag}
            onDoubleClick={showMainWindow}
            aria-hidden="true"
          />
        ))}
        <button
          className="bubble-orb"
          disabled={!backendRunning || !state.enabled}
          onPointerDown={startPointerRecording}
          onPointerUp={stopPointerRecording}
          onDoubleClick={showMainWindow}
          aria-label="悬浮录音"
        >
          <span className="bubble-glow" />
          <span className="bubble-core">{state.recording ? <Pause size={18} /> : <Mic2 size={20} />}</span>
        </button>
      </main>
    );
  }

  return (
    <main
      className={`shell glass-${appearance.mode} ${appearance.acrylic ? "material-acrylic" : "material-solid"} ${state.recording ? "is-recording" : ""} ${!state.enabled ? "is-off" : ""}`}
      style={shellStyle}
    >
      <div className="aura aura-blue" />
      <div className="aura aura-gold" />
      <div className="aura aura-mint" />
      <div className="noise" />

      <section className="window">
        <header className="topbar glass">
          <div className="title-drag-zone" onPointerDown={startWindowDrag}>
            <div className="identity">
              <img src="/brand/app_icon.png" alt="" />
              <div>
                <h1>言栖</h1>
                <p>Liquid Voice Input</p>
              </div>
            </div>
            <div className="status-pill">
              <span className="status-dot" />
              {serviceLabel}
            </div>
          </div>
          <div className="window-controls" onPointerDown={stopChromePointer}>
            <button type="button" onClick={minimizeWindow} aria-label="最小化">
              <Minus size={15} />
            </button>
            <button type="button" onClick={closeWindow} aria-label="关闭">
              <X size={15} />
            </button>
          </div>
        </header>

        <section className="hero glass-deep">
          <div className="hero-copy">
            <span className="eyebrow">
              <Sparkles size={14} />
              Local SenseVoice
            </span>
            <h2>{state.recording ? "Listening" : state.enabled ? "Ready" : "Paused"}</h2>
            <p>
              {state.recording
                ? "松开按键或再次点击完成识别。"
                : state.enabled
                  ? `按住 ${shortcut.label}，说完自动粘贴到光标位置。`
                  : "语音输入已暂停，点击启用恢复。"}
            </p>
          </div>

          <button
            className="orb"
            disabled={!backendRunning}
            onPointerDown={startPointerRecording}
            onPointerUp={stopPointerRecording}
            aria-label="开始录音"
          >
            <span className="orb-ripple" />
            <span className="orb-core">
              {state.recording ? <Pause size={26} /> : <Mic2 size={31} />}
            </span>
          </button>
        </section>

        <section className="telemetry">
          <div className="mini glass">
            <ShieldCheck size={18} />
            <div>
              <strong>{state.exclusive ? "独占优先" : "共享模式"}</strong>
              <span>{state.mic_guarded ? "默认麦克风已隔离" : state.audio_mode}</span>
            </div>
          </div>
        </section>

        <section className="result glass">
          <div className="section-title">
            <AudioLines size={17} />
            <span>识别结果</span>
            <button className="icon-button" onClick={copyResult} disabled={!state.last_text}>
              {copied ? <Check size={17} /> : <Copy size={17} />}
            </button>
          </div>
          <p className={state.last_text ? "result-text" : "result-text placeholder"}>{resultText}</p>
        </section>

        <footer className="dock glass">
          <button type="button" onClick={toggleEnabled} className={state.enabled ? "dock-action active" : "dock-action"}>
            <Power size={18} />
            {state.enabled ? "暂停" : "启用"}
          </button>
          <button type="button" onClick={() => setSettingsOpen(true)} className="dock-action">
            <Settings size={18} />
            设置
          </button>
          <button type="button" onClick={() => setAppearanceOpen(true)} className="dock-action">
            <Palette size={18} />
            外观
          </button>
        </footer>

        {settingsOpen && (
          <div className="settings-sheet glass">
            <div className="sheet-head">
              <div>
                <span className="eyebrow">
                  <SlidersHorizontal size={14} />
                  Controls
                </span>
                <h3>设置</h3>
              </div>
              <button className="icon-button" onClick={() => setSettingsOpen(false)}>
                <X size={18} />
              </button>
            </div>

            <label className="toggle-row">
              <span>
                <strong>录音时独占设备</strong>
                <em>优先阻止会议软件旁听</em>
              </span>
              <input
                type="checkbox"
                checked={state.exclusive}
                onChange={(e) => post("/api/config", { exclusive_device: e.currentTarget.checked })}
              />
            </label>

            <label className="toggle-row">
              <span>
                <strong>悬浮气泡</strong>
                <em>关闭主窗口后保留桌面控制</em>
              </span>
              <input
                type="checkbox"
                checked={state.floating_bubble}
                onChange={(e) => post("/api/config", { floating_bubble: e.currentTarget.checked })}
              />
            </label>

            <div className={capturingShortcut ? "shortcut-row capturing" : "shortcut-row"}>
              <span>
                <strong>录音快捷键</strong>
                <em>{capturingShortcut ? "按下键盘或鼠标按钮" : shortcut.label}</em>
              </span>
              <div className="shortcut-actions">
                <button
                  type="button"
                  className="shortcut-pill"
                  data-shortcut-control="true"
                  onClick={() => setCapturingShortcut((value) => !value)}
                >
                  {capturingShortcut ? <MousePointer2 size={15} /> : <Keyboard size={15} />}
                  {capturingShortcut ? "等待输入" : "录制"}
                </button>
                <button
                  type="button"
                  className="shortcut-icon"
                  data-shortcut-control="true"
                  onClick={() => void applyShortcut(defaultShortcut).catch(() => setShortcut(defaultShortcut))}
                  aria-label="恢复默认快捷键"
                >
                  <RotateCcw size={15} />
                </button>
              </div>
            </div>

            <div className="device-list">
              <span className="list-label">输入设备</span>
              {devices.map((device) => (
                <button
                  key={device.index}
                  className={state.input_device_index === device.index ? "device active" : "device"}
                  onClick={() => post("/api/config", { input_device_index: device.index })}
                >
                  <Mic size={16} />
                  <span>{device.name}</span>
                  {device.default && <em>默认</em>}
                </button>
              ))}
              {!devices.length && <p className="empty">未检测到麦克风或后端未连接。</p>}
            </div>
          </div>
        )}

        {appearanceOpen && (
          <div className="settings-sheet appearance-sheet glass">
            <div className="sheet-head">
              <div>
                <span className="eyebrow">
                  <Palette size={14} />
                  Appearance
                </span>
                <h3>外观</h3>
              </div>
              <button className="icon-button" onClick={() => setAppearanceOpen(false)}>
                <X size={18} />
              </button>
            </div>

            <div className="appearance-group">
              <span className="list-label">风格</span>
              <div className="segmented">
                <button
                  className={appearance.mode === "liquid" ? "segment active" : "segment"}
                  onClick={() => updateAppearance({ mode: "liquid" })}
                >
                  液态玻璃
                </button>
                <button
                  className={appearance.mode === "clear" ? "segment active" : "segment"}
                  onClick={() => updateAppearance({ mode: "clear" })}
                >
                  清透玻璃
                </button>
              </div>
            </div>

            <div className="appearance-group">
              <span className="list-label">主题色</span>
              <div className="swatches">
                {accentOptions.map((accent) => (
                  <button
                    key={accent}
                    className={appearance.accent === accent ? "swatch active" : "swatch"}
                    style={{ background: accent }}
                    onClick={() => updateAppearance({ accent })}
                    aria-label={`主题色 ${accent}`}
                  />
                ))}
              </div>
            </div>

            <label className="range-row">
              <span>
                <strong>透明度</strong>
                <em>{appearance.opacity}%</em>
              </span>
              <input
                type="range"
                min="0"
                max="100"
                value={appearance.opacity}
                onChange={(event) => updateAppearance({ opacity: Number(event.currentTarget.value) })}
              />
            </label>

            <label className="toggle-row">
              <span>
                <strong>亚克力材质</strong>
                <em>开启后使用背景模糊和高光折射</em>
              </span>
              <input
                type="checkbox"
                checked={appearance.acrylic}
                onChange={(event) => updateAppearance({ acrylic: event.currentTarget.checked })}
              />
            </label>
          </div>
        )}
      </section>
    </main>
  );
}

export default App;
