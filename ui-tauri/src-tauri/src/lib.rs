use std::{
    collections::hash_map::DefaultHasher,
    fs,
    hash::{Hash, Hasher},
    io::{Read, Write},
    net::{Ipv4Addr, SocketAddr, SocketAddrV4, TcpStream},
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, AtomicU16, AtomicU64, Ordering},
        mpsc, Mutex, OnceLock,
    },
    thread,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

#[cfg(target_os = "windows")]
use std::os::windows::ffi::OsStrExt;

use tauri::{
    image::Image,
    menu::{Menu, MenuItem},
    tray::{MouseButton, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, Manager, WebviewUrl, WebviewWindowBuilder, WindowEvent,
};

mod product;
use product::{
    app_data_dir, APP_VERSION, BACKEND_SIDECAR_NAME, BUNDLED_BACKEND_NAME, COPYRIGHT,
    LEGACY_BACKEND_SIDECAR_NAME, LEGACY_BUNDLED_BACKEND_NAME,
};

#[cfg(target_os = "windows")]
use windows_sys::Win32::{
    Foundation::{LPARAM, LRESULT, WPARAM},
    Media::Audio::{PlaySoundW, SND_FILENAME, SND_NODEFAULT, SND_SYNC, SND_SYSTEM},
    UI::{
        Input::KeyboardAndMouse::{
            GetAsyncKeyState, VK_CONTROL, VK_LBUTTON, VK_MBUTTON, VK_RBUTTON, VK_XBUTTON1,
            VK_XBUTTON2,
        },
        WindowsAndMessaging::{
            CallNextHookEx, GetMessageW, SetWindowsHookExW, HC_ACTION, KBDLLHOOKSTRUCT, MSG,
            MSLLHOOKSTRUCT, WH_KEYBOARD_LL, WH_MOUSE_LL, WM_KEYDOWN, WM_KEYUP, WM_LBUTTONDOWN,
            WM_LBUTTONUP, WM_MBUTTONDOWN, WM_MBUTTONUP, WM_RBUTTONDOWN, WM_RBUTTONUP,
            WM_SYSKEYDOWN, WM_SYSKEYUP, WM_XBUTTONDOWN, WM_XBUTTONUP,
        },
    },
};

struct BackendProcess {
    child: Mutex<Option<Child>>,
    backend_path: PathBuf,
    backend_token: String,
    port: u16,
    closing: AtomicBool,
}

const VK_BACK_CODE: u32 = 0x08;
const VK_TAB_CODE: u32 = 0x09;
const VK_RETURN_CODE: u32 = 0x0D;
const VK_SHIFT_CODE: u32 = 0x10;
const VK_CONTROL_CODE: u32 = 0x11;
const VK_MENU_CODE: u32 = 0x12;
const VK_ESCAPE_CODE: u32 = 0x1B;
const VK_SPACE_CODE: u32 = 0x20;
const VK_PRIOR_CODE: u32 = 0x21;
const VK_NEXT_CODE: u32 = 0x22;
const VK_END_CODE: u32 = 0x23;
const VK_HOME_CODE: u32 = 0x24;
const VK_LEFT_CODE: u32 = 0x25;
const VK_UP_CODE: u32 = 0x26;
const VK_RIGHT_CODE: u32 = 0x27;
const VK_DOWN_CODE: u32 = 0x28;
const VK_INSERT_CODE: u32 = 0x2D;
const VK_DELETE_CODE: u32 = 0x2E;
const VK_LWIN_CODE: u32 = 0x5B;
const VK_RWIN_CODE: u32 = 0x5C;
const VK_F1_CODE: u32 = 0x70;
const VK_F9_CODE: u32 = 0x78;
const VK_F24_CODE: u32 = 0x87;
const VK_LSHIFT_CODE: u32 = 0xA0;
const VK_RSHIFT_CODE: u32 = 0xA1;
const VK_LCONTROL_CODE: u32 = 0xA2;
const VK_RCONTROL_CODE: u32 = 0xA3;
const VK_LMENU_CODE: u32 = 0xA4;
const VK_RMENU_CODE: u32 = 0xA5;

#[derive(Clone, serde::Serialize, serde::Deserialize)]
struct ShortcutConfig {
    keys: Vec<u32>,
    mouse_buttons: Vec<u16>,
    label: String,
}

impl Default for ShortcutConfig {
    fn default() -> Self {
        Self {
            keys: vec![VK_RCONTROL_CODE],
            mouse_buttons: Vec::new(),
            label: "Right Ctrl".to_string(),
        }
    }
}

#[derive(Default)]
struct ShortcutState {
    ctrl_held: bool,
    shift_held: bool,
    pressed_keys: Vec<u32>,
    pressed_mouse_buttons: Vec<u16>,
    shortcut_active: bool,
    recording: bool,
}

#[derive(Clone, Copy)]
enum PromptSound {
    Start,
    Done,
    ToggleOn,
    ToggleOff,
    Error,
}

static BACKEND_PORT: AtomicU16 = AtomicU16::new(47632);
static LAST_TOGGLE_MS: AtomicU64 = AtomicU64::new(0);
static BACKEND_ENABLED: AtomicBool = AtomicBool::new(true);
static BACKEND_RECORDING: AtomicBool = AtomicBool::new(false);
static APP_HANDLE: OnceLock<AppHandle> = OnceLock::new();
static SOUND_TX: OnceLock<mpsc::Sender<PromptSound>> = OnceLock::new();
static SHORTCUT_STATE: OnceLock<Mutex<ShortcutState>> = OnceLock::new();
static SHORTCUT_CONFIG: OnceLock<Mutex<ShortcutConfig>> = OnceLock::new();
static BACKEND_AUTH_TOKEN: OnceLock<String> = OnceLock::new();

impl Drop for BackendProcess {
    fn drop(&mut self) {
        stop_backend(self);
    }
}

#[derive(serde::Serialize)]
struct BackendInfo {
    port: u16,
    backend_path: String,
    backend_token: String,
    app_data_dir: String,
    version: String,
    running: bool,
}

#[derive(Clone, serde::Serialize)]
struct RecordingChanged {
    recording: bool,
}

fn shortcut_state() -> &'static Mutex<ShortcutState> {
    SHORTCUT_STATE.get_or_init(|| Mutex::new(ShortcutState::default()))
}

fn shortcut_config_store() -> &'static Mutex<ShortcutConfig> {
    SHORTCUT_CONFIG.get_or_init(|| Mutex::new(load_shortcut_config().unwrap_or_default()))
}

fn shortcut_config_path() -> PathBuf {
    app_data_dir().join("shortcut.json")
}

fn legacy_shortcut_config_path() -> Option<PathBuf> {
    std::env::var_os("APPDATA")
        .map(PathBuf::from)
        .map(|path| path.join("YanQi").join("shortcut.json"))
}

fn migrate_legacy_shortcut_config(path: &PathBuf) {
    if path.exists() {
        return;
    }
    let Some(legacy_path) = legacy_shortcut_config_path() else {
        return;
    };
    if !legacy_path.exists() {
        return;
    }
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let _ = fs::copy(legacy_path, path);
}

fn shortcut_label(config: &ShortcutConfig) -> String {
    let mut parts = config
        .keys
        .iter()
        .map(|code| key_label(*code))
        .collect::<Vec<_>>();
    parts.extend(
        config
            .mouse_buttons
            .iter()
            .map(|button| mouse_button_label(*button)),
    );
    parts.join(" + ")
}

fn key_label(code: u32) -> String {
    match code {
        VK_BACK_CODE => "Backspace".to_string(),
        VK_TAB_CODE => "Tab".to_string(),
        VK_RETURN_CODE => "Enter".to_string(),
        VK_ESCAPE_CODE => "Esc".to_string(),
        VK_SPACE_CODE => "Space".to_string(),
        VK_PRIOR_CODE => "Page Up".to_string(),
        VK_NEXT_CODE => "Page Down".to_string(),
        VK_END_CODE => "End".to_string(),
        VK_HOME_CODE => "Home".to_string(),
        VK_LEFT_CODE => "Left".to_string(),
        VK_UP_CODE => "Up".to_string(),
        VK_RIGHT_CODE => "Right".to_string(),
        VK_DOWN_CODE => "Down".to_string(),
        VK_INSERT_CODE => "Insert".to_string(),
        VK_DELETE_CODE => "Delete".to_string(),
        VK_SHIFT_CODE => "Shift".to_string(),
        VK_CONTROL_CODE => "Ctrl".to_string(),
        VK_MENU_CODE => "Alt".to_string(),
        VK_LSHIFT_CODE => "Left Shift".to_string(),
        VK_RSHIFT_CODE => "Right Shift".to_string(),
        VK_LCONTROL_CODE => "Left Ctrl".to_string(),
        VK_RCONTROL_CODE => "Right Ctrl".to_string(),
        VK_LMENU_CODE => "Left Alt".to_string(),
        VK_RMENU_CODE => "Right Alt".to_string(),
        VK_LWIN_CODE => "Left Win".to_string(),
        VK_RWIN_CODE => "Right Win".to_string(),
        0x30..=0x39 | 0x41..=0x5A => char::from_u32(code).unwrap_or('?').to_string(),
        VK_F1_CODE..=VK_F24_CODE => format!("F{}", code - VK_F1_CODE + 1),
        _ => format!("VK {code}"),
    }
}

fn mouse_button_label(button: u16) -> String {
    match button {
        1 => "Mouse Left".to_string(),
        2 => "Mouse Right".to_string(),
        3 => "Mouse Middle".to_string(),
        4 => "Mouse Back".to_string(),
        5 => "Mouse Forward".to_string(),
        _ => format!("Mouse {button}"),
    }
}

fn normalize_shortcut_config(mut config: ShortcutConfig) -> Result<ShortcutConfig, String> {
    config.keys.retain(|code| (1..=255).contains(code));
    config.keys.sort_unstable();
    config.keys.dedup();

    config
        .mouse_buttons
        .retain(|button| (1..=5).contains(button));
    config.mouse_buttons.sort_unstable();
    config.mouse_buttons.dedup();

    if config.mouse_buttons.len() > 1 {
        return Err("一次只能绑定一个鼠标按键".to_string());
    }
    if config.keys.is_empty() && config.mouse_buttons.is_empty() {
        return Err("快捷键不能为空".to_string());
    }

    config.label = config.label.trim().to_string();
    if config.label.is_empty() {
        config.label = shortcut_label(&config);
    }

    Ok(config)
}

fn load_shortcut_config() -> Option<ShortcutConfig> {
    let path = shortcut_config_path();
    migrate_legacy_shortcut_config(&path);
    let content = fs::read_to_string(path).ok()?;
    serde_json::from_str::<ShortcutConfig>(&content)
        .ok()
        .and_then(|config| normalize_shortcut_config(config).ok())
}

fn save_shortcut_config(config: &ShortcutConfig) -> Result<(), String> {
    let path = shortcut_config_path();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|err| err.to_string())?;
    }
    let content = serde_json::to_string_pretty(config).map_err(|err| err.to_string())?;
    fs::write(path, content).map_err(|err| err.to_string())
}

fn current_shortcut_config() -> ShortcutConfig {
    shortcut_config_store()
        .lock()
        .map(|config| config.clone())
        .unwrap_or_default()
}

fn reset_shortcut_input_state() -> bool {
    shortcut_state()
        .lock()
        .map(|mut state| {
            let was_recording = state.recording;
            *state = ShortcutState::default();
            was_recording
        })
        .unwrap_or(false)
}

fn update_pressed_u32(values: &mut Vec<u32>, value: u32, pressed: bool) {
    if pressed {
        if !values.contains(&value) {
            values.push(value);
        }
    } else {
        values.retain(|candidate| *candidate != value);
    }
}

fn update_pressed_u16(values: &mut Vec<u16>, value: u16, pressed: bool) {
    if pressed {
        if !values.contains(&value) {
            values.push(value);
        }
    } else {
        values.retain(|candidate| *candidate != value);
    }
}

fn shortcut_key_down(state: &ShortcutState, code: u32) -> bool {
    match code {
        VK_CONTROL_CODE => state.pressed_keys.iter().any(|pressed| {
            matches!(
                *pressed,
                VK_CONTROL_CODE | VK_LCONTROL_CODE | VK_RCONTROL_CODE
            )
        }),
        VK_SHIFT_CODE => state
            .pressed_keys
            .iter()
            .any(|pressed| matches!(*pressed, VK_SHIFT_CODE | VK_LSHIFT_CODE | VK_RSHIFT_CODE)),
        VK_MENU_CODE => state
            .pressed_keys
            .iter()
            .any(|pressed| matches!(*pressed, VK_MENU_CODE | VK_LMENU_CODE | VK_RMENU_CODE)),
        _ => state.pressed_keys.contains(&code),
    }
}

fn shortcut_inputs_down(state: &ShortcutState, config: &ShortcutConfig) -> bool {
    config
        .keys
        .iter()
        .all(|code| shortcut_key_down(state, *code))
        && config
            .mouse_buttons
            .iter()
            .all(|button| state.pressed_mouse_buttons.contains(button))
}

fn refresh_modifier_state(state: &mut ShortcutState) {
    state.ctrl_held = state
        .pressed_keys
        .iter()
        .any(|code| matches!(*code, VK_CONTROL_CODE | VK_LCONTROL_CODE | VK_RCONTROL_CODE));
    state.shift_held = state
        .pressed_keys
        .iter()
        .any(|code| matches!(*code, VK_SHIFT_CODE | VK_LSHIFT_CODE | VK_RSHIFT_CODE));
}

fn backend_script_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("voice_backend.py")
}

fn backend_sidecar_candidates() -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    if let Ok(current_exe) = std::env::current_exe() {
        if let Some(exe_dir) = current_exe.parent() {
            candidates.push(exe_dir.join(BACKEND_SIDECAR_NAME));
            candidates.push(exe_dir.join(BUNDLED_BACKEND_NAME));
            candidates.push(exe_dir.join(LEGACY_BACKEND_SIDECAR_NAME));
            candidates.push(exe_dir.join(LEGACY_BUNDLED_BACKEND_NAME));
            candidates.push(exe_dir.join("resources").join(BACKEND_SIDECAR_NAME));
            candidates.push(exe_dir.join("resources").join(BUNDLED_BACKEND_NAME));
            candidates.push(
                exe_dir
                    .join("..")
                    .join("resources")
                    .join(BACKEND_SIDECAR_NAME),
            );
            candidates.push(
                exe_dir
                    .join("..")
                    .join("resources")
                    .join(BUNDLED_BACKEND_NAME),
            );
        }
    }

    candidates.push(
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("bin")
            .join(BACKEND_SIDECAR_NAME),
    );

    candidates
}

fn spawn_sidecar(path: &PathBuf, port: u16, token: &str) -> Option<Child> {
    let mut command = Command::new(path);
    if let Some(parent) = path.parent() {
        command.current_dir(parent);
    }
    command
        .arg("--no-hotkeys")
        .arg("--silent-sounds")
        .arg("--port")
        .arg(port.to_string())
        .arg("--token")
        .arg(token)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|err| {
            eprintln!("failed to start Vernest sidecar {}: {err}", path.display());
            err
        })
        .ok()
}

fn spawn_python_backend(script_path: &PathBuf, port: u16, token: &str) -> Option<Child> {
    Command::new("python")
        .arg(script_path)
        .arg("--no-hotkeys")
        .arg("--silent-sounds")
        .arg("--port")
        .arg(port.to_string())
        .arg("--token")
        .arg(token)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|err| {
            eprintln!("failed to start Vernest backend: {err}");
            err
        })
        .ok()
}

fn spawn_backend(port: u16, token: &str) -> (Option<Child>, PathBuf) {
    for path in backend_sidecar_candidates() {
        if path.exists() {
            if let Some(child) = spawn_sidecar(&path, port, token) {
                return (Some(child), path);
            }
        }
    }

    let script_path = backend_script_path();
    if !script_path.exists() {
        eprintln!("Vernest backend script missing: {}", script_path.display());
        return (None, script_path);
    }

    (spawn_python_backend(&script_path, port, token), script_path)
}

fn stop_backend(process: &BackendProcess) {
    let child = detach_backend_child(process);
    stop_backend_parts(child, process.port);
}

fn detach_backend_child(process: &BackendProcess) -> Option<Child> {
    process.child.lock().ok().and_then(|mut guard| guard.take())
}

fn stop_backend_parts(child: Option<Child>, port: u16) {
    if let Some(mut child) = child {
        let _ = child.kill();
        let _ = child.wait();
    }
    stop_processes_on_port(port);
}

fn is_backend_port_open(port: u16) -> bool {
    let addr = SocketAddr::V4(SocketAddrV4::new(Ipv4Addr::LOCALHOST, port));
    TcpStream::connect_timeout(&addr, Duration::from_millis(180)).is_ok()
}

#[cfg(target_os = "windows")]
fn stop_processes_on_port(port: u16) {
    let Ok(output) = Command::new("netstat")
        .args(["-ano", "-p", "tcp"])
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .output()
    else {
        return;
    };

    let marker = format!(":{}", port);
    let current_pid = std::process::id().to_string();
    let stdout = String::from_utf8_lossy(&output.stdout);
    for line in stdout.lines() {
        if !line.contains(&marker) || !line.contains("LISTENING") {
            continue;
        }

        let Some(pid) = line.split_whitespace().last() else {
            continue;
        };
        if pid == current_pid {
            continue;
        }

        let _ = Command::new("taskkill")
            .args(["/PID", pid, "/F", "/T"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
}

#[cfg(not(target_os = "windows"))]
fn stop_processes_on_port(_port: u16) {}

fn response_body(response: &str) -> &str {
    response
        .split_once("\r\n\r\n")
        .map(|(_, body)| body)
        .unwrap_or(response)
}

fn parse_backend_response(response: String) -> Result<serde_json::Value, String> {
    serde_json::from_str::<serde_json::Value>(response_body(&response))
        .map_err(|err| err.to_string())
}

fn backend_auth_token() -> &'static str {
    BACKEND_AUTH_TOKEN.get().map(String::as_str).unwrap_or("")
}

fn generate_backend_token() -> String {
    let base = format!(
        "{}:{}:{:?}",
        std::process::id(),
        now_ms(),
        SystemTime::now()
    );
    let mut token = String::with_capacity(64);
    for salt in 0..4_u8 {
        let mut hasher = DefaultHasher::new();
        base.hash(&mut hasher);
        salt.hash(&mut hasher);
        token.push_str(&format!("{:016x}", hasher.finish()));
    }
    token
}

fn post_backend_json(path: &'static str, port: u16) -> Result<serde_json::Value, String> {
    let addr = SocketAddr::V4(SocketAddrV4::new(Ipv4Addr::LOCALHOST, port));
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_millis(500))
        .map_err(|err| err.to_string())?;

    let _ = stream.set_write_timeout(Some(Duration::from_millis(700)));
    let _ = stream.set_read_timeout(Some(Duration::from_millis(700)));
    let request = format!(
        "POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\nX-Vernest-Token: {}\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{{}}",
        backend_auth_token()
    );
    stream
        .write_all(request.as_bytes())
        .map_err(|err| err.to_string())?;
    let mut response = String::new();
    stream
        .read_to_string(&mut response)
        .map_err(|err| err.to_string())?;
    parse_backend_response(response)
}

fn get_backend(path: &'static str, port: u16) -> Option<String> {
    let addr = SocketAddr::V4(SocketAddrV4::new(Ipv4Addr::LOCALHOST, port));
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(500)) else {
        return None;
    };

    let _ = stream.set_write_timeout(Some(Duration::from_millis(700)));
    let _ = stream.set_read_timeout(Some(Duration::from_millis(700)));
    let request =
        format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Vernest-Token: {}\r\nConnection: close\r\n\r\n", backend_auth_token());
    if stream.write_all(request.as_bytes()).is_err() {
        return None;
    }

    let mut response = String::new();
    stream.read_to_string(&mut response).ok()?;
    Some(response)
}

fn get_backend_json(path: &'static str, port: u16) -> Option<serde_json::Value> {
    get_backend(path, port).and_then(|response| parse_backend_response(response).ok())
}

fn backend_floating_bubble_enabled(port: u16) -> bool {
    get_backend_json("/api/status", port)
        .and_then(|value| value.get("state")?.get("floating_bubble")?.as_bool())
        .unwrap_or(false)
}

#[cfg(target_os = "windows")]
fn prompt_sound_bytes(sound: PromptSound) -> &'static [u8] {
    match sound {
        PromptSound::Start => include_bytes!("../sounds/start.wav"),
        PromptSound::Done => include_bytes!("../sounds/done.wav"),
        PromptSound::ToggleOn => include_bytes!("../sounds/toggle_on.wav"),
        PromptSound::ToggleOff => include_bytes!("../sounds/toggle_off.wav"),
        PromptSound::Error => include_bytes!("../sounds/error.wav"),
    }
}

#[cfg(target_os = "windows")]
fn prompt_sound_name(sound: PromptSound) -> &'static str {
    match sound {
        PromptSound::Start => "start.wav",
        PromptSound::Done => "done.wav",
        PromptSound::ToggleOn => "toggle_on.wav",
        PromptSound::ToggleOff => "toggle_off.wav",
        PromptSound::Error => "error.wav",
    }
}

#[cfg(target_os = "windows")]
fn prepare_prompt_sound(sound: PromptSound) -> Vec<u16> {
    let dir = std::env::temp_dir().join("vernest-tauri-sounds-v1");
    let _ = fs::create_dir_all(&dir);
    let path = dir.join(prompt_sound_name(sound));
    let bytes = prompt_sound_bytes(sound);
    let rewrite = fs::metadata(&path)
        .map(|metadata| metadata.len() != bytes.len() as u64)
        .unwrap_or(true);
    if rewrite {
        let _ = fs::write(&path, bytes);
    }
    path.as_os_str().encode_wide().chain([0]).collect()
}

#[cfg(target_os = "windows")]
fn play_prompt_sound_now(sound: PromptSound) {
    let path = prepare_prompt_sound(sound);
    unsafe {
        let _ = PlaySoundW(
            path.as_ptr(),
            std::ptr::null_mut(),
            SND_FILENAME | SND_SYNC | SND_NODEFAULT | SND_SYSTEM,
        );
    };
}

#[cfg(target_os = "windows")]
fn start_sound_worker() {
    let (tx, rx) = mpsc::channel::<PromptSound>();
    if SOUND_TX.set(tx).is_err() {
        return;
    }
    thread::spawn(move || {
        for sound in rx {
            play_prompt_sound_now(sound);
        }
    });
}

#[cfg(target_os = "windows")]
fn play_prompt_sound(sound: PromptSound) {
    if let Some(tx) = SOUND_TX.get() {
        let _ = tx.send(sound);
    } else {
        play_prompt_sound_now(sound);
    }
}

#[cfg(not(target_os = "windows"))]
fn play_prompt_sound(_sound: PromptSound) {}

#[cfg(not(target_os = "windows"))]
fn start_sound_worker() {}

fn emit_recording_changed(recording: bool) {
    if let Some(app) = APP_HANDLE.get() {
        let _ = app.emit("recording-changed", RecordingChanged { recording });
    }
}

fn emit_backend_state(payload: &serde_json::Value) {
    if let Some(app) = APP_HANDLE.get() {
        let _ = app.emit("backend-state", payload.clone());
    }
}

fn state_value(payload: &serde_json::Value) -> Option<&serde_json::Value> {
    payload.get("state")
}

fn update_runtime_state(payload: &serde_json::Value) {
    let Some(state) = state_value(payload) else {
        return;
    };

    if let Some(enabled) = state.get("enabled").and_then(|value| value.as_bool()) {
        BACKEND_ENABLED.store(enabled, Ordering::Relaxed);
    }

    if let Some(recording) = state.get("recording").and_then(|value| value.as_bool()) {
        let previous = BACKEND_RECORDING.swap(recording, Ordering::Relaxed);
        if previous != recording {
            emit_recording_changed(recording);
        }
    }
}

fn publish_backend_payload(payload: &serde_json::Value) {
    update_runtime_state(payload);
    emit_backend_state(payload);
}

fn event_timestamp(payload: &serde_json::Value) -> Option<f64> {
    state_value(payload)?
        .get("last_event_at")
        .and_then(|value| value.as_f64())
}

fn event_name(payload: &serde_json::Value) -> Option<&str> {
    state_value(payload)?
        .get("last_event")
        .and_then(|value| value.as_str())
}

fn prompt_for_backend_event(event: &str) -> Option<PromptSound> {
    match event {
        "error" => Some(PromptSound::Error),
        _ => None,
    }
}

fn start_backend_monitor(port: u16) {
    thread::spawn(move || {
        let mut last_event_at: Option<f64> = None;
        let mut last_payload: Option<String> = None;
        loop {
            thread::sleep(Duration::from_millis(220));
            let Some(payload) = get_backend_json("/api/status", port) else {
                continue;
            };

            let payload_signature = payload.to_string();
            if last_payload.as_deref() != Some(payload_signature.as_str()) {
                publish_backend_payload(&payload);
                last_payload = Some(payload_signature);
            }

            let Some(event_at) = event_timestamp(&payload) else {
                continue;
            };
            let should_play = last_event_at
                .map(|previous| event_at > previous + f64::EPSILON)
                .unwrap_or(false);
            last_event_at = Some(event_at);

            if should_play {
                if let Some(sound) = event_name(&payload).and_then(prompt_for_backend_event) {
                    play_prompt_sound(sound);
                }
            }
        }
    });
}

fn request_start_recording(port: u16) -> Result<serde_json::Value, String> {
    if !is_backend_port_open(port) {
        play_prompt_sound(PromptSound::Error);
        return Err("backend is not reachable".into());
    }

    if !BACKEND_ENABLED.load(Ordering::Relaxed) {
        if let Some(payload) = get_backend_json("/api/status", port) {
            publish_backend_payload(&payload);
            if !state_value(&payload)
                .and_then(|state| state.get("enabled"))
                .and_then(|value| value.as_bool())
                .unwrap_or(false)
            {
                return Ok(payload);
            }
        }
    }

    BACKEND_RECORDING.store(true, Ordering::Relaxed);
    emit_recording_changed(true);
    play_prompt_sound(PromptSound::Start);

    match post_backend_json("/api/start", port) {
        Ok(payload) => {
            publish_backend_payload(&payload);
            Ok(payload)
        }
        Err(err) => {
            BACKEND_RECORDING.store(false, Ordering::Relaxed);
            emit_recording_changed(false);
            play_prompt_sound(PromptSound::Error);
            Err(err)
        }
    }
}

fn request_stop_recording(port: u16) -> Result<serde_json::Value, String> {
    let was_recording = BACKEND_RECORDING.swap(false, Ordering::Relaxed);
    emit_recording_changed(false);
    if was_recording {
        play_prompt_sound(PromptSound::Done);
    }

    match post_backend_json("/api/stop", port) {
        Ok(payload) => {
            publish_backend_payload(&payload);
            Ok(payload)
        }
        Err(err) => {
            play_prompt_sound(PromptSound::Error);
            Err(err)
        }
    }
}

fn request_toggle_enabled(port: u16) -> Result<serde_json::Value, String> {
    match post_backend_json("/api/toggle", port) {
        Ok(payload) => {
            publish_backend_payload(&payload);
            let enabled = state_value(&payload)
                .and_then(|state| state.get("enabled"))
                .and_then(|value| value.as_bool())
                .unwrap_or_else(|| BACKEND_ENABLED.load(Ordering::Relaxed));
            play_prompt_sound(if enabled {
                PromptSound::ToggleOn
            } else {
                PromptSound::ToggleOff
            });
            Ok(payload)
        }
        Err(err) => {
            play_prompt_sound(PromptSound::Error);
            Err(err)
        }
    }
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis() as u64)
        .unwrap_or(0)
}

enum ShortcutAction {
    Start,
    Stop,
    Toggle,
}

fn shortcut_transition(
    state: &mut ShortcutState,
    config: &ShortcutConfig,
) -> Option<ShortcutAction> {
    let active = shortcut_inputs_down(state, config);
    if active && !state.shortcut_active {
        state.shortcut_active = true;
        if !state.recording && BACKEND_ENABLED.load(Ordering::Relaxed) {
            state.recording = true;
            return Some(ShortcutAction::Start);
        }
    } else if !active && state.shortcut_active {
        state.shortcut_active = false;
        if state.recording {
            state.recording = false;
            return Some(ShortcutAction::Stop);
        }
    }
    None
}

fn dispatch_shortcut_action(action: ShortcutAction) {
    let port = BACKEND_PORT.load(Ordering::Relaxed);
    thread::spawn(move || {
        let _ = match action {
            ShortcutAction::Start => request_start_recording(port),
            ShortcutAction::Stop => request_stop_recording(port),
            ShortcutAction::Toggle => request_toggle_enabled(port),
        };
    });
}

fn finish_shortcut_update(action: Option<ShortcutAction>) {
    if let Some(action) = action {
        match action {
            ShortcutAction::Start => emit_recording_changed(true),
            ShortcutAction::Stop => emit_recording_changed(false),
            ShortcutAction::Toggle => {}
        }
        dispatch_shortcut_action(action);
    }
}

fn handle_shortcut_key(vk_code: u32, pressed: bool) {
    let mut action: Option<ShortcutAction> = None;
    let config = current_shortcut_config();

    if let Ok(mut state) = shortcut_state().lock() {
        update_pressed_u32(&mut state.pressed_keys, vk_code, pressed);
        refresh_modifier_state(&mut state);

        if vk_code == VK_F9_CODE && pressed && state.ctrl_held && state.shift_held {
            let now = now_ms();
            let previous = LAST_TOGGLE_MS.load(Ordering::Relaxed);
            if now.saturating_sub(previous) > 350 {
                LAST_TOGGLE_MS.store(now, Ordering::Relaxed);
                action = Some(ShortcutAction::Toggle);
            }
        }

        if action.is_none() {
            action = shortcut_transition(&mut state, &config);
        }
    }

    finish_shortcut_update(action);
}

fn handle_shortcut_mouse(button: u16, pressed: bool) {
    let mut action: Option<ShortcutAction> = None;
    let config = current_shortcut_config();

    if let Ok(mut state) = shortcut_state().lock() {
        update_pressed_u16(&mut state.pressed_mouse_buttons, button, pressed);
        action = shortcut_transition(&mut state, &config);
    }

    finish_shortcut_update(action);
}

#[cfg(target_os = "windows")]
fn normalize_control_key(vk_code: u32, flags: u32, pressed: bool) -> u32 {
    if vk_code == u32::from(VK_CONTROL) {
        let right_control_down = shortcut_state()
            .lock()
            .map(|state| state.pressed_keys.contains(&VK_RCONTROL_CODE))
            .unwrap_or(false);
        if flags & 0x01 != 0 || (!pressed && right_control_down) {
            VK_RCONTROL_CODE
        } else {
            VK_LCONTROL_CODE
        }
    } else if vk_code == VK_MENU_CODE {
        if flags & 0x01 != 0 {
            VK_RMENU_CODE
        } else {
            VK_LMENU_CODE
        }
    } else {
        vk_code
    }
}

#[cfg(target_os = "windows")]
unsafe extern "system" fn keyboard_proc(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    if code == HC_ACTION as i32 {
        let message = wparam as u32;
        let pressed = message == WM_KEYDOWN || message == WM_SYSKEYDOWN;
        let released = message == WM_KEYUP || message == WM_SYSKEYUP;
        if pressed || released {
            let event = unsafe { &*(lparam as *const KBDLLHOOKSTRUCT) };
            let vk_code = normalize_control_key(event.vkCode, event.flags, pressed);
            handle_shortcut_key(vk_code, pressed);
        }
    }
    unsafe { CallNextHookEx(std::ptr::null_mut(), code, wparam, lparam) }
}

#[cfg(target_os = "windows")]
fn mouse_button_from_message(message: u32, mouse_data: u32) -> Option<(u16, bool)> {
    match message {
        WM_LBUTTONDOWN => Some((1, true)),
        WM_LBUTTONUP => Some((1, false)),
        WM_RBUTTONDOWN => Some((2, true)),
        WM_RBUTTONUP => Some((2, false)),
        WM_MBUTTONDOWN => Some((3, true)),
        WM_MBUTTONUP => Some((3, false)),
        WM_XBUTTONDOWN | WM_XBUTTONUP => {
            let x_button = ((mouse_data >> 16) & 0xffff) as u16;
            match x_button {
                1 => Some((4, message == WM_XBUTTONDOWN)),
                2 => Some((5, message == WM_XBUTTONDOWN)),
                _ => None,
            }
        }
        _ => None,
    }
}

#[cfg(target_os = "windows")]
unsafe extern "system" fn mouse_proc(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    if code == HC_ACTION as i32 {
        let event = unsafe { &*(lparam as *const MSLLHOOKSTRUCT) };
        if let Some((button, pressed)) = mouse_button_from_message(wparam as u32, event.mouseData) {
            handle_shortcut_mouse(button, pressed);
        }
    }
    unsafe { CallNextHookEx(std::ptr::null_mut(), code, wparam, lparam) }
}

#[cfg(target_os = "windows")]
fn start_keyboard_hook(port: u16) {
    BACKEND_PORT.store(port, Ordering::Relaxed);
    thread::spawn(|| unsafe {
        let keyboard_hook =
            SetWindowsHookExW(WH_KEYBOARD_LL, Some(keyboard_proc), std::ptr::null_mut(), 0);
        if keyboard_hook.is_null() {
            eprintln!("failed to install Vernest keyboard hook");
        }

        let mouse_hook = SetWindowsHookExW(WH_MOUSE_LL, Some(mouse_proc), std::ptr::null_mut(), 0);
        if mouse_hook.is_null() {
            eprintln!("failed to install Vernest mouse hook");
        }

        if keyboard_hook.is_null() && mouse_hook.is_null() {
            return;
        }

        let mut message: MSG = std::mem::zeroed();
        while GetMessageW(&mut message, std::ptr::null_mut(), 0, 0) > 0 {}
    });
}

#[cfg(target_os = "windows")]
fn shortcut_key_physically_down(vk_code: u32) -> bool {
    unsafe { (GetAsyncKeyState(vk_code as i32) as u16) & 0x8000 != 0 }
}

#[cfg(target_os = "windows")]
fn mouse_button_vk(button: u16) -> Option<i32> {
    match button {
        1 => Some(i32::from(VK_LBUTTON)),
        2 => Some(i32::from(VK_RBUTTON)),
        3 => Some(i32::from(VK_MBUTTON)),
        4 => Some(i32::from(VK_XBUTTON1)),
        5 => Some(i32::from(VK_XBUTTON2)),
        _ => None,
    }
}

#[cfg(target_os = "windows")]
fn shortcut_mouse_physically_down(button: u16) -> bool {
    mouse_button_vk(button)
        .map(|vk| unsafe { (GetAsyncKeyState(vk) as u16) & 0x8000 != 0 })
        .unwrap_or(false)
}

#[cfg(target_os = "windows")]
fn start_shortcut_release_watcher() {
    thread::spawn(|| loop {
        thread::sleep(Duration::from_millis(70));
        let config = current_shortcut_config();
        let mut should_stop = false;
        if let Ok(mut state) = shortcut_state().lock() {
            state
                .pressed_keys
                .retain(|code| shortcut_key_physically_down(*code));
            state
                .pressed_mouse_buttons
                .retain(|button| shortcut_mouse_physically_down(*button));
            refresh_modifier_state(&mut state);

            if state.shortcut_active && !shortcut_inputs_down(&state, &config) {
                state.shortcut_active = false;
                if state.recording {
                    state.recording = false;
                    should_stop = true;
                }
            }
        }

        if should_stop {
            emit_recording_changed(false);
            let port = BACKEND_PORT.load(Ordering::Relaxed);
            thread::spawn(move || {
                let _ = request_stop_recording(port);
            });
        }
    });
}

#[cfg(not(target_os = "windows"))]
fn start_keyboard_hook(port: u16) {
    BACKEND_PORT.store(port, Ordering::Relaxed);
}

#[cfg(not(target_os = "windows"))]
fn start_shortcut_release_watcher() {}

#[tauri::command]
fn backend_info(state: tauri::State<'_, BackendProcess>) -> BackendInfo {
    let child_running = state
        .child
        .lock()
        .map(|mut guard| match guard.as_mut() {
            Some(child) => match child.try_wait() {
                Ok(Some(_)) => {
                    *guard = None;
                    false
                }
                Ok(None) => true,
                Err(err) => {
                    eprintln!("failed to inspect Vernest backend: {err}");
                    false
                }
            },
            None => false,
        })
        .unwrap_or(false);
    let running = child_running || is_backend_port_open(state.port);
    BackendInfo {
        port: state.port,
        backend_path: state.backend_path.to_string_lossy().to_string(),
        backend_token: state.backend_token.clone(),
        app_data_dir: app_data_dir().to_string_lossy().to_string(),
        version: APP_VERSION.to_string(),
        running,
    }
}

#[tauri::command]
fn minimize_window(window: tauri::Window) -> Result<(), String> {
    window.minimize().map_err(|err| err.to_string())
}

#[tauri::command]
fn start_window_drag(window: tauri::Window) -> Result<(), String> {
    window.start_dragging().map_err(|err| err.to_string())
}

#[tauri::command]
fn start_recording() -> Result<serde_json::Value, String> {
    request_start_recording(BACKEND_PORT.load(Ordering::Relaxed))
}

#[tauri::command]
fn stop_recording() -> Result<serde_json::Value, String> {
    request_stop_recording(BACKEND_PORT.load(Ordering::Relaxed))
}

#[tauri::command]
fn toggle_enabled() -> Result<serde_json::Value, String> {
    request_toggle_enabled(BACKEND_PORT.load(Ordering::Relaxed))
}

#[tauri::command]
fn get_shortcut_config() -> ShortcutConfig {
    current_shortcut_config()
}

#[tauri::command]
fn set_shortcut_config(config: ShortcutConfig) -> Result<ShortcutConfig, String> {
    let normalized = normalize_shortcut_config(config)?;
    save_shortcut_config(&normalized)?;

    if let Ok(mut current) = shortcut_config_store().lock() {
        *current = normalized.clone();
    }

    if reset_shortcut_input_state() {
        emit_recording_changed(false);
        let port = BACKEND_PORT.load(Ordering::Relaxed);
        thread::spawn(move || {
            let _ = request_stop_recording(port);
        });
    }

    Ok(normalized)
}

fn bubble_position(window: Option<&tauri::Window>) -> Option<(f64, f64)> {
    let monitor = window.and_then(|window| window.current_monitor().ok().flatten())?;
    let scale = monitor.scale_factor();
    let position = monitor.position();
    let size = monitor.size();
    let x = position.x as f64 / scale + size.width as f64 / scale - 96.0;
    let y = position.y as f64 / scale + size.height as f64 / scale - 124.0;
    Some((x.max(12.0), y.max(12.0)))
}

fn show_bubble_window(
    app: &AppHandle,
    source_window: Option<&tauri::Window>,
) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("bubble") {
        window.show().map_err(|err| err.to_string())?;
        return Ok(());
    }

    let mut builder = WebviewWindowBuilder::new(
        app,
        "bubble",
        WebviewUrl::App("index.html?view=bubble".into()),
    )
    .title("言栖悬浮气泡")
    .inner_size(74.0, 74.0)
    .resizable(false)
    .decorations(false)
    .transparent(true)
    .shadow(false)
    .skip_taskbar(true)
    .always_on_top(true);

    if let Some((x, y)) = bubble_position(source_window) {
        builder = builder.position(x, y);
    }

    builder.build().map_err(|err| err.to_string())?;
    Ok(())
}

fn hide_bubble_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("bubble") {
        let _ = window.hide();
    }
}

fn restore_main_window(app: &AppHandle) {
    hide_bubble_window(app);
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn center_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.center();
    }
}

#[tauri::command]
fn show_main_window(app: AppHandle) {
    restore_main_window(&app);
}

fn redact_log_line(line: &str) -> String {
    if line.contains("已粘贴") || line.contains("[本地]") || line.contains("result") {
        return "[redacted recognition content]".to_string();
    }
    line.to_string()
}

fn read_recent_log() -> String {
    let path = app_data_dir().join("logs").join("vernest.log");
    let Ok(content) = fs::read_to_string(path) else {
        return "log file not found".to_string();
    };
    let mut lines = content
        .lines()
        .rev()
        .take(240)
        .map(redact_log_line)
        .collect::<Vec<_>>();
    lines.reverse();
    lines.join("\n")
}

#[tauri::command]
fn export_diagnostics(state: tauri::State<'_, BackendProcess>) -> Result<String, String> {
    let dir = app_data_dir().join("diagnostics");
    fs::create_dir_all(&dir).map_err(|err| err.to_string())?;
    let path = dir.join(format!("vernest-diagnostics-{}.txt", now_ms()));
    let running = is_backend_port_open(state.port);
    let content = format!(
        "Vernest diagnostics\n\
         version: {APP_VERSION}\n\
         copyright: {COPYRIGHT}\n\
         app_data_dir: {}\n\
         backend_path: {}\n\
         backend_port: {}\n\
         backend_running: {}\n\
         privacy: fully local; no diagnostic upload is performed by the app\n\n\
         recent_log:\n{}\n",
        app_data_dir().display(),
        state.backend_path.display(),
        state.port,
        running,
        read_recent_log()
    );
    fs::write(&path, content).map_err(|err| err.to_string())?;
    Ok(path.to_string_lossy().to_string())
}

fn close_to_tray_async(app: AppHandle, window: tauri::Window, fallback_show_bubble: bool) {
    let port = BACKEND_PORT.load(Ordering::Relaxed);
    thread::spawn(move || {
        let show_bubble = if is_backend_port_open(port) {
            backend_floating_bubble_enabled(port)
        } else {
            fallback_show_bubble
        };

        let app_for_ui = app.clone();
        let window_for_ui = window.clone();
        let _ = app.run_on_main_thread(move || {
            if show_bubble {
                let _ = show_bubble_window(&app_for_ui, Some(&window_for_ui));
            } else {
                hide_bubble_window(&app_for_ui);
            }
            let _ = window_for_ui.hide();
        });
    });
}

#[tauri::command]
fn close_to_tray(app: AppHandle, window: tauri::Window, show_bubble: bool) -> Result<(), String> {
    close_to_tray_async(app, window, show_bubble);
    Ok(())
}

fn quit_app(app: &AppHandle) {
    if let Some(state) = app.try_state::<BackendProcess>() {
        state.closing.store(true, Ordering::SeqCst);
        stop_backend(state.inner());
    }
    app.exit(0);
}

fn setup_tray(app: &mut tauri::App) -> tauri::Result<()> {
    let show_item = MenuItem::with_id(app, "show", "显示主窗口", true, None::<&str>)?;
    let quit_item = MenuItem::with_id(app, "quit", "退出言栖", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show_item, &quit_item])?;
    let icon = Image::new(include_bytes!("../icons/tray-32.rgba"), 32, 32);

    TrayIconBuilder::with_id("vernest-tray")
        .tooltip("言栖")
        .icon(icon)
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "show" => restore_main_window(app),
            "quit" => quit_app(app),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| match event {
            TrayIconEvent::Click {
                button: MouseButton::Left,
                ..
            }
            | TrayIconEvent::DoubleClick {
                button: MouseButton::Left,
                ..
            } => restore_main_window(tray.app_handle()),
            _ => {}
        })
        .build(app)?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalizes_shortcut_input() {
        let config = ShortcutConfig {
            keys: vec![VK_RCONTROL_CODE, VK_RCONTROL_CODE, 999],
            mouse_buttons: vec![2, 2],
            label: "".to_string(),
        };

        let normalized = normalize_shortcut_config(config).expect("shortcut should be valid");
        assert_eq!(normalized.keys, vec![VK_RCONTROL_CODE]);
        assert_eq!(normalized.mouse_buttons, vec![2]);
        assert_eq!(normalized.label, "Right Ctrl + Mouse Right");
    }

    #[test]
    fn rejects_empty_shortcut() {
        let config = ShortcutConfig {
            keys: vec![],
            mouse_buttons: vec![],
            label: "".to_string(),
        };

        assert!(normalize_shortcut_config(config).is_err());
    }

    #[test]
    fn redacts_recognition_log_content() {
        assert_eq!(
            redact_log_line("[12:00:00] 已粘贴: hello"),
            "[redacted recognition content]"
        );
        assert_eq!(
            redact_log_line("[12:00:00] backend ready"),
            "[12:00:00] backend ready"
        );
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let port = 47632;
    let backend_token = generate_backend_token();
    let _ = BACKEND_AUTH_TOKEN.set(backend_token.clone());
    let (child, backend_path) = spawn_backend(port, &backend_token);

    tauri::Builder::default()
        .manage(BackendProcess {
            child: Mutex::new(child),
            backend_path,
            backend_token,
            port,
            closing: AtomicBool::new(false),
        })
        .setup(move |app| {
            let _ = APP_HANDLE.set(app.handle().clone());
            let _ = shortcut_config_store();
            start_sound_worker();
            start_keyboard_hook(port);
            start_shortcut_release_watcher();
            start_backend_monitor(port);
            setup_tray(app)?;
            center_main_window(app.handle());
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "bubble" {
                    api.prevent_close();
                    let _ = window.hide();
                    return;
                }
                if let Some(state) = window.try_state::<BackendProcess>() {
                    if !state.closing.load(Ordering::SeqCst) {
                        api.prevent_close();
                        close_to_tray_async(window.app_handle().clone(), window.clone(), false);
                    }
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            backend_info,
            minimize_window,
            close_to_tray,
            start_recording,
            stop_recording,
            toggle_enabled,
            get_shortcut_config,
            set_shortcut_config,
            show_main_window,
            start_window_drag,
            export_diagnostics
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
