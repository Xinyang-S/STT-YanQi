# Cloud Text Polishing Plan

Vernest no longer ships or runs a local LLM text-polishing model.

The planned polishing feature is cloud-provider based and is not available in
the current release. The intended product model is:

- The user explicitly enables cloud text polishing.
- The user selects a provider, enters their own API key, and chooses a model.
- Vernest sends only the current transcript text to the selected provider for
  polishing.
- Audio is not uploaded for polishing.
- The request prompt is owned by Vernest, with provider-specific request adapters
  behind a common interface.
- Failed polishing must never block recognition, paste, or prompt sounds.

## Target Providers

The provider layer should support official APIs first:

- OpenAI
- Anthropic
- Google Gemini
- DeepSeek
- Alibaba Qwen
- Moonshot
- Zhipu GLM
- OpenRouter or another OpenAI-compatible endpoint as an advanced option

## Prompt Direction

The system prompt should be conservative:

- Preserve the user's intent, facts, logic, negation, conditions, numbers,
  names, code, paths, commands, and language mix.
- Fix punctuation, segmentation, casing, obvious ASR homophones, and filler
  words only when confidence is high.
- Do not translate unless the user explicitly asks to translate.
- Do not summarize, expand, infer missing details, or replace the user's
  decision.
- If uncertain, return the original transcript.

## Current Release Behavior

The current release exposes only a disabled UI preview for this feature. There
is no API-key storage, no provider call path, and no local model fallback.
