# Local LLM Research for STT Text Polishing

Date: 2026-06-14

Goal: add an optional text polishing stage after STT. The current implementation
is local-first, but the product no longer makes an absolute no-network promise.
The model should be as small as practical, run on ordinary Windows laptops, and
avoid reasoning/thinking behavior because this is a deterministic text cleanup
task rather than a reasoning task.

## Task Definition

Input:

- Short STT output from SenseVoice
- Usually Chinese, sometimes English or mixed Chinese/English
- No external context

Output:

- Same meaning as the STT text
- Better punctuation, sentence breaks, and light cleanup of obvious ASR errors
- No added facts
- No explanation, only the final polished text

## Spoken-Language Cleanup Findings

Spontaneous speech is not just "messy writing". It contains interaction and
planning artifacts that should be handled conservatively:

- Filled pauses and hesitation markers: Chinese `嗯`, `呃`, `啊`; English
  `um`, `uh`, `er`. They are often removable when they only mark planning, but
  should be preserved when they carry response meaning.
- Repetitions and false starts: speakers often begin a phrase, abandon it, and
  restart with a clearer version.
- Self-repairs: speakers correct themselves inline, e.g. "go to Shanghai, no,
  Beijing". For dictation, the final corrected version is usually the intended
  written text.
- Editing terms and discourse markers: words like "就是", "然后", "like",
  "you know" may be removable only when they add no semantic content.
- Code-switching: bilingual users may intentionally mix Chinese and English.
  The post-processor should preserve the language mix and not translate
  technical terms, product names, code, paths, commands, or abbreviations.

This means the prompt should not ask for broad rewriting. It should ask for
speech-to-written cleanup while preserving intent, facts, language mix, and
uncertain tokens.

## ASR Error Findings

ASR output commonly needs post-processing in several separate dimensions:

- Punctuation restoration and sentence segmentation.
- Capitalization for English.
- Inverse text normalization (spoken-form to written-form formatting for
  numbers, dates, units, URLs, and similar entities).
- Disfluency removal or repair.
- Conservative correction of obvious recognition errors, especially homophones,
  missing/extra words, and near-sound substitutions.
- Higher risk around named entities, professional terms, acronyms, code-switched
  segments, and domain terms. These should be preserved unless the correction is
  obvious from context.

The important product rule is fail-open: if the local LLM is unavailable or the
output is suspicious, Vernest must paste the raw STT text instead of blocking
input or inventing content.

## Prompt Design Derived From Research

The prompt should frame the model as an ASR post-processor, not a general editor.
The required behavior is:

- Output only the final text.
- Preserve intent, language, facts, names, numbers, units, commands, code,
  paths, and domain terms.
- Fix punctuation, sentence breaks, capitalization, and obvious recognition
  errors.
- Preserve intentional Chinese-English code-switching.
- Remove only clearly non-semantic fillers and obvious repeated starts.
- For self-correction or restart patterns, keep the final clear version.
- Do not translate, summarize, expand, explain, or add facts.
- When uncertain, keep the original wording.

Because the selected 0.5B local model can still mis-handle short self-repair
spans, Vernest also uses a narrow deterministic pre-hint for high-confidence
Chinese repair patterns such as `去上海不对去北京`: when the corrected span starts
with the same recent action cue, the rejected short span is removed before LLM
punctuation cleanup. Ambiguous negative sentences are left unchanged.

## Selection Criteria

- Small parameter count and small quantized file size
- Good Chinese and multilingual instruction following
- Works with local CPU inference
- Clear commercial-friendly license
- Official or first-party GGUF / llama.cpp path preferred
- No thinking mode, or at least no need to manage thinking tokens
- Failure must be safe: if unavailable, Vernest pastes the raw STT text

## Candidates

| Model | Size | Strengths | Concerns | Decision |
|---|---:|---|---|---|
| Qwen2.5-0.5B-Instruct GGUF | 0.49B, Q4_K_M file 491,400,032 bytes | Strong Chinese coverage, instruction tuned, Apache-2.0, official GGUF repo, q2/q3/q4/q5/q6/q8 options | Slightly larger than 270M/360M models | Selected |
| Qwen3-0.6B GGUF | 0.6B | Newer Qwen generation, 100+ languages, Apache-2.0 | Adds thinking/non-thinking mode control; official GGUF page currently emphasizes q8_0, which is heavier than Q4 | Not first choice |
| SmolLM2-360M-Instruct | 360M | Very small, Apache-2.0, on-device oriented | Official card says models primarily understand/generate English; Chinese STT polish is a core use case for Vernest | Not selected |
| Gemma 3 270M | 270M | Smallest candidate, multilingual, strong instruction-following for size | Requires accepting Google's usage license on Hugging Face; license/redistribution path is less simple than Apache-2.0; not Chinese-first | Not selected |
| Llama 3.2 1B Instruct | 1B | Strong mobile/on-device positioning | Larger, gated access/contact sharing, custom Llama license, officially supported languages do not include Chinese | Not selected |

## Final Choice

Use `Qwen/Qwen2.5-0.5B-Instruct-GGUF` with:

```text
qwen2.5-0.5b-instruct-q4_k_m.gguf
```

Rationale:

- The model is small enough for desktop CPU use while staying more reliable for Chinese than the smaller English-oriented candidates.
- The official Qwen model card lists multilingual support including Chinese and English.
- The GGUF repo is official and documents llama.cpp usage and quantization variants.
- Apache-2.0 is simpler for a commercial desktop app than gated/custom-license alternatives.
- Qwen2.5 does not require managing a thinking mode for this simple rewrite task.

## Runtime Plan

- Use `llama-cpp-python` to load the local GGUF file.
- Keep the model as a lazy-loaded singleton inside the Python sidecar.
- Default `polish_enabled=false`; users must opt in because the small local
  model can still alter critical logic in edge cases.
- If dependency or model file is missing, expose the status to UI and paste the raw STT text.
- Keep temperature low and max output short to reduce hallucination.

## Source Links

- Disfluencies as intra-utterance dialogue moves: <https://semprag.org/index.php/sp/article/download/sp.7.9/pdfsp79>
- Hesitation disfluencies in spontaneous speech: <https://www.research.ed.ac.uk/files/15012157/Corley_Stewart_2008.pdf>
- Transition-Based Disfluency Detection using LSTMs: <https://aclanthology.org/D17-1296/>
- Four-in-One ASR post-processing: <https://arxiv.org/abs/2210.15063>
- Dual Language Models for Code Switched Speech Recognition: <https://www.isca-archive.org/interspeech_2018/garg18_interspeech.html>
- QASR speech corpus with segmentation, punctuation, and NER tasks: <https://aclanthology.org/2021.acl-long.177/>
- NVIDIA NeMo text processing / ITN: <https://github.com/NVIDIA/NeMo-text-processing>
- Qwen2.5-0.5B-Instruct: <https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct>
- Qwen2.5-0.5B-Instruct-GGUF: <https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF>
- Qwen3-0.6B-GGUF: <https://huggingface.co/Qwen/Qwen3-0.6B-GGUF>
- SmolLM2-360M-Instruct: <https://huggingface.co/HuggingFaceTB/SmolLM2-360M-Instruct>
- Gemma 3 270M: <https://huggingface.co/google/gemma-3-270m>
- Llama 3.2 1B Instruct: <https://huggingface.co/meta-llama/Llama-3.2-1B-Instruct>
- llama-cpp-python API: <https://llama-cpp-python.readthedocs.io/en/latest/api-reference/>
