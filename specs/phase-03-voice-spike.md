# Phase 3 — Voice Pipeline Feasibility Spike

**Status:** Decision recorded — **browser Web Speech API only** (no Pipecat, no native speech packages)
**Source:** `specs/roadmap.md` Phase 3, item 1

## Purpose

Record PC specs, confirm `pipecat-ai` installs and imports on Python 3.14, and write this spec with the chosen stack. No product code is written in this phase.

## Decision

**Voice v1 uses the browser's built-in Web Speech API** (SpeechRecognition for input, SpeechSynthesis for output).

**Rationale:** All native speech packages (Pipecat transports, `daily-python`, `openai-whisper`/`torch`, `piper-tts`, `onnxruntime`) are blocked on this machine by an **Application Control policy** (Smart App Control / WDAC / AppLocker) that refuses to load DLLs from user-writable paths (`WinError 4551`). The VC++ 2015-2022 redistributable is also missing. Remediation requires admin rights and is not pursued in this spike.

**Consequences:**
- Pipecat is **not used** in v1.
- No new Python dependencies, no Node/npm, no native packages.
- STT and TTS run entirely in the browser via `webkitSpeechRecognition` / `SpeechRecognition` and `speechSynthesis`.
- Audio never reaches the server — only transcripts and TTS text travel over HTTP.

---

## Hardware & Environment

| Item | Value |
|------|-------|
| **CPU** | Intel Core i5-5200U @ 2.20 GHz (2 cores / 4 threads) |
| **RAM** | 8.5 GB total (≈1.1 GB free at idle) |
| **GPU** | Intel HD Graphics 5500 (1 GB VRAM, no CUDA) |
| **Disk** | D: 64 GB free (project lives here) |
| **OS** | Windows 11 Pro 10.0.26200 (64-bit) |
| **Python (venv)** | 3.14.8, MSC v.1944 64-bit (AMD64), **not** free-threaded |
| **Build tools** | **No MSVC / cl.exe on PATH** — native extensions must come as wheels |

---

## Package Availability on Python 3.14 (Windows)

| Package | Latest | `requires_python` | Windows wheel for cp314? |
|---------|--------|-------------------|--------------------------|
| `pipecat-ai` | 1.13.0 | `>=3.11` | **Yes** — `pipecat_ai-1.13.0-py3-none-any.whl` |
| `pipecat-ai-flows` | 1.4.0 | `>=3.11` | **Yes** — `pipecat_ai_flows-1.4.0-py3-none-any.whl` |
| `aiortc` | 1.15.0 | `>=3.10` | **No** — sdist only (`aiortc-1.15.0.tar.gz`) |
| `webrtcvad` | 2.0.10 | (none) | **No** — sdist only |
| `av` | 19.0.2 | `>=3.12` | **Yes** — `av-19.0.2-cp314-cp314t-win_amd64.whl` |
| `numpy` | 2.5.4 | `>=3.12` | **Yes** — cp314 wheels exist |
| `ctranslate2` | 4.8.3 | `>=3.9` | **Yes** — cp314 wheels exist |
| `onnxruntime` | 1.31.0 | `>=3.11` | **Yes** — cp314 wheels exist |
| `torch` | 2.14.1 | `>=3.10` | **Yes** — cp314 wheels exist |
| `faster-whisper` | 1.2.1 | `>=3.9` | sdist (but deps have wheels) |
| `piper-tts` | 1.8.0 | `>=3.9` | cp39-abi3 wheel (should work) |
| `pyaudio` | 0.2.14 | — | Wheels up to cp313 only |

**Critical finding:** `aiortc` and `webrtcvad` are **sdist-only**. They require a C++ compiler (MSVC) to build native extensions. This machine has **no build tools**, so they will fail to install unless pre-built wheels appear on PyPI.

---

## Blockers (empirical)

| Blocker | Evidence | Fix requires |
|---------|----------|--------------|
| **Missing VC++ 2015-2022 redistributable** | No `msvcp140.dll` / `vcruntime140.dll` in `C:\Windows\System32`. PyTorch error: *"Microsoft Visual C++ Redistributable is not installed."* | Admin: install `https://aka.ms/vs/17/release/vc_redist.x64.exe` |
| **Application Control policy (Smart App Control / WDAC / AppLocker)** | Every native import fails with `OSError: [WinError 4551] An Application Control policy has blocked this file` for DLLs under `D:\voice-chatbot\.venv\Lib\site-packages\...` | Admin: disable Smart App Control or create WDAC rule allowing the venv path |

**Affected packages (all fail identically):** `daily`, `onnxruntime`, `torch`, `whisper` (depends on torch), `piper`.

## Tradeoffs of Web Speech API

| Aspect | Detail |
|--------|--------|
| **STT location** | Runs in the browser vendor's cloud service (Google Cloud Speech for Chrome/Edge, Apple for Safari). Not on-device. |
| **Browser support** | SpeechRecognition: Chrome 25+, Edge 79+, Safari 14.1+ (partial), Firefox **not supported** (behind flag, no continuous mode). SpeechSynthesis: universal. Source: MDN <https://developer.mozilla.org/en-US/docs/Web/API/SpeechRecognition#browser_compatibility> |
| **Audio privacy** | Raw audio never reaches our server — only the transcript text. Browser vendor receives audio for STT. |
| **Latency measurement** | Measured in-browser per stage (recognition latency, synthesis queue time, network RTT to `/api/v1/voice/turn`). Server only sees text round-trip. |
| **Barge-in (interruption)** | Implemented in JS: cancel `speechSynthesis` when `onstart` / `onresult` fires while speaking. |
| **No streaming** | Web Speech API delivers final/partial transcripts; no byte-streaming. |

## Fallback Stack (what we actually build)

| Layer | Choice | Why |
|-------|--------|-----|
| **Transport** | Plain HTTPS (FastAPI static file + `POST /api/v1/voice/turn`) | Zero native deps; works everywhere |
| **STT** | Browser `SpeechRecognition` (Web Speech API) | No Python dependency; runs in browser |
| **TTS** | Browser `speechSynthesis` (Web Speech API) | No Python dependency; runs in browser |
| **VAD / Barge-in** | JS: cancel synthesis on user speech start | No Python dependency |
| **Agent brain** | Existing `AgentLoop` via new endpoint | Reuses Phase 2b logic |
| **Disclosure** | Spoken first ("This call may be recorded...") via `speechSynthesis` | Regulatory requirement |

---

## Files to Create / Modify (this spike only)

```
specs/phase-03-voice-spike.md   ← this file
```

No backend code changes in this phase.