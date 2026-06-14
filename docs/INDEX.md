# Vernest Project Knowledge Base

`docs/` is the project knowledge base. Keep decisions, implementation notes,
release assumptions, and future work here instead of leaving them only in chat.

## Core Documents

- [ARCHITECTURE.md](ARCHITECTURE.md): runtime split, process boundaries, and known module debt.
- [DEVELOPMENT.md](DEVELOPMENT.md): local setup, build commands, data directories, and regression focus.
- [PRIVACY.md](PRIVACY.md): local-first privacy position and commercial privacy draft.
- [RELEASE.md](RELEASE.md): installer, portable package, signing, and updater notes.
- [CLOUD_POLISH_PLAN.md](CLOUD_POLISH_PLAN.md): future cloud text-polishing provider plan.

## Current Product Direction

- Chinese name: 言栖
- English name: Vernest
- Target OS: Windows 10 / Windows 11 x64
- Default posture: local-first, no automatic upload, optional network features must be explicit
- Desktop shell: Tauri + React
- System integration: Rust host owns tray, global shortcuts, prompt sounds, windows, and sidecar lifecycle
- Voice runtime: Python sidecar owns recording, SenseVoice ASR, device enumeration, and paste

## Knowledge Base Rules

- Add a Markdown note when a decision affects packaging, privacy, model choice, runtime architecture, or user data.
- Prefer source links for model, dependency, license, and release-process decisions.
- Keep commercial release gaps explicit; do not hide beta limitations in README only.
