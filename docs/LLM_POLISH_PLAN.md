# STT Text Polishing Implementation Plan

## Product Behavior

- The polishing feature is enabled by default.
- Users can manually turn it on or off in Settings.
- When enabled and the local model is available, Vernest sends STT output to a
  local LLM and pastes the polished text.
- When disabled, Vernest pastes the raw STT output.
- When enabled but the model or runtime dependency is unavailable, Vernest does
  not block voice input. It reports the missing capability in UI and pastes raw
  STT output.

## Current Pipeline

```text
record audio
  -> SenseVoice ASR
  -> raw_text
  -> optional local LLM polish
  -> last_text
  -> paste_text(last_text)
```

The implementation point is `voice_core/runtime.py` inside `recording_flow()`,
after `ASRManager.transcribe()` returns and before `paste_text()`.

## Configuration

Stored in `%APPDATA%\Vernest\config.json`:

```json
{
  "polish_enabled": false,
  "polish_model_path": "",
  "polish_max_tokens": 160,
  "polish_temperature": 0.1
}
```

`polish_model_path` can override the default search path. If empty, Vernest
searches:

```text
<bundled resources>\models\polish\
<current working directory>\models\polish\
%APPDATA%\Vernest\models\polish\
```

## Model

Selected model:

```text
Qwen/Qwen2.5-0.5B-Instruct-GGUF
qwen2.5-0.5b-instruct-q4_k_m.gguf
```

Download helper:

```powershell
.\scripts\download-polish-model.ps1
```

Compatibility fallback for a user-installed model under `%APPDATA%\Vernest\models\polish\`:

```powershell
.\scripts\download-polish-model.ps1 -AppData
```

Default local path:

```text
models\polish\qwen2.5-0.5b-instruct-q4_k_m.gguf
```

## Prompt Contract

System intent:

- Treat the task as local ASR post-processing, not broad rewriting.
- Only output polished text.
- Preserve original language mix, meaning, facts, names, numbers, units, code,
  paths, commands, and professional terms.
- Do not translate, summarize, expand, explain, or add facts.
- Fix punctuation, sentence breaks, English capitalization, and obvious
  missing/extra/homophone-like recognition errors.
- Preserve intentional Chinese-English code-switching.
- Remove only clearly non-semantic fillers and obvious repeated starts.
- For self-correction or restart patterns, keep the final clear version.
- Return unchanged text when already good or when the correction is uncertain.

Runtime safeguards:

- Low temperature
- Short `max_tokens`
- Strip code fences, labels, and thinking tags if any appear
- Fall back to raw text if output is empty or unexpectedly much longer than input
- Apply a narrow deterministic self-repair hint before the LLM only when a
  correction marker such as `不对`/`不是` is followed by a high-confidence action
  cue already present before the marker. This handles common "said X, no, Y"
  dictation without asking the small model to infer the repair span from
  scratch.

## Memory Behavior

- `polish_enabled=false` is the default. The GGUF model is not loaded unless the
  user explicitly enables polishing and a real STT result needs polishing.
- `polish_enabled=true` still does not preload or reserve the GGUF model at app
  startup. The runtime only checks whether the dependency and model file exist.
- The Qwen GGUF is lazy-loaded on the first actual STT polishing request.
- After first use, the model is kept as a shared singleton to avoid reloading on
  every utterance.
- When the user turns polishing off in Settings, Vernest unloads the shared
  model and runs garbage collection. Future STT output bypasses the LLM until
  polishing is enabled again.
- If polishing is off before the first recording, the LLM model is never loaded
  and the backend memory footprint stays lower.

Local measurement on the development Windows machine:

| State | Working Set | Private Bytes |
|---|---:|---:|
| Python runtime before config | ~38 MB | ~506 MB |
| After config/status checks | ~38 MB | ~506 MB |
| After loading Qwen2.5-0.5B Q4_K_M | ~185 MB | ~970 MB |
| After first inference | ~554 MB | ~975 MB |

The model file itself is about 491 MB. Actual app memory also includes the STT
engine and audio runtime.

## Regression Test Coverage

`tests/test_polish_regression.py` contains one case for each researched scene:

- Chinese and English filled pauses
- Repeated starts
- False starts/restarts
- Chinese and English self-repairs
- Discourse markers
- Chinese-English command/code switching
- Punctuation and sentence segmentation
- English capitalization
- Number/date/unit written-form handling
- URL/email/path preservation
- Named entity and professional term preservation
- Homophone/near-sound correction
- Negative preservation
- Already-good text

Default command:

```powershell
python -m unittest discover -s tests
```

Optional real-model regression:

```powershell
$env:VERNEST_RUN_LLM_POLISH_TESTS = "1"
python -m unittest tests.test_polish_regression
```

## API Surface

`/api/status` exposes:

```json
{
  "raw_text": "",
  "last_text": "",
  "polish_enabled": false,
  "polish_available": false,
  "polish_model": "qwen2.5-0.5b-instruct-q4_k_m.gguf",
  "polish_last_error": ""
}
```

`/api/config` accepts:

```json
{
  "polish_enabled": false
}
```

## Packaging Notes

- `llama-cpp-python` is listed in `requirements.txt`.
- `requirements.txt` uses the official CPU wheel index
  `https://abetlen.github.io/llama-cpp-python/whl/cpu` to avoid Windows source
  builds.
- PyInstaller declares `llama_cpp` as a hidden import.
- `.gguf` files are ignored by git.
- The portable zip includes the selected GGUF file automatically when it exists
  under `models\polish\` during `build-release.ps1`.
- The installer includes the selected GGUF file as a Tauri resource when it
  exists under `models\polish\` during `build-release.ps1`.
- `%APPDATA%\Vernest\models\polish\` remains a fallback only, not the primary
  release location.

## Regression Checklist

- App starts when `llama-cpp-python` is not installed.
- App starts when the GGUF model is missing.
- Settings can turn text polishing on/off.
- With polishing disabled, `last_text` equals raw STT output.
- With polishing enabled but model unavailable, raw STT output is pasted and UI reports the missing model/runtime.
- With model available, `raw_text` stores STT output and `last_text` stores polished output.
- Failed polishing never prevents result reporting or `paste_text()`.
- Existing shortcut, tray, floating bubble, and prompt sound flows remain unchanged.
