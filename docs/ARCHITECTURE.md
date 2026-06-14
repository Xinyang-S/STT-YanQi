# Vernest Architecture

Vernest is split into three layers:

1. React UI in `ui-tauri/src`
2. Rust desktop host in `ui-tauri/src-tauri/src`
3. Python sidecar in `voice_backend.py` and `voice_core/`

The Rust host owns OS integration: transparent windows, tray, global keyboard
and mouse hooks, prompt sounds, close-to-tray behavior, and sidecar lifecycle.

The Python sidecar owns audio capture, device enumeration, ASR, paste behavior,
STT text polishing, and voice configuration. It listens on `127.0.0.1:47632`.

Each app launch generates a local-only token. The sidecar requires
`X-Vernest-Token` for control APIs. `/api/health` is intentionally unauthenticated
for smoke checks.

Current module debt to continue reducing:

- Split `ui-tauri/src-tauri/src/lib.rs` into host modules.
- Split `ui-tauri/src/App.tsx` into settings, appearance, shell, and bubble views.
- Move remaining hard-coded Chinese copy behind `src/i18n.ts`.

## Recognition Pipeline

```text
AudioRecorder
  -> ASRManager / sherpa-onnx SenseVoice
  -> raw_text
  -> LocalTextPolisher / llama-cpp-python / Qwen2.5-0.5B GGUF
  -> last_text
  -> paste_text()
```

The polishing stage is optional and fail-open. If `llama-cpp-python` or the
GGUF model is unavailable, Vernest keeps the STT result and continues the paste
flow.
