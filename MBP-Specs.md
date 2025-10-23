# Mac AI Workstation Cheatsheet (M4 Max, 48 GB)

This folder documents how your Mac is optimized for AI/ML work and gives quick toggles/scripts you can run anytime. It’s also a ready-to-paste context for new repos so collaborators understand your local environment.

## Snapshot
- Hardware: Apple M4 Max, 48 GB unified memory, 40‑core GPU
- OS: macOS 26.0.1; Xcode 26 installed (Metal toolchain present; `metallib` missing – optional fix)
- Runtimes: Ollama (Metal), PyTorch (MPS), MLX (Apple Silicon), Core ML tools
- Python ML env: `~/.venvs/ml-mps` (Python 3.12) with Torch 2.9, HF stack, MLX 0.29+, coremltools
- Measured (llama3.1:8b on your device): ~157 tok/s prompt eval, ~87 tok/s generation

## Quick Toggles
- AI Mode (performance): start Ollama, disable Low Power, keep awake
  - Script: `bash Desktop/AI-Cheatsheet/scripts/ai-on.sh`
  - Alias: `ai-on`
- Chill/Movie Mode (quiet/low drain): stop Ollama, enable Low Power, allow sleep
  - Script: `bash Desktop/AI-Cheatsheet/scripts/ai-off.sh`
  - Alias: `ai-off`
- Status: Ollama/port/power/caffeinate quick check
  - Script: `bash Desktop/AI-Cheatsheet/scripts/ai-status.sh`
  - Alias: `ai-status`

Aliases installed in `~/.zshrc` automatically. Reload your shell once: `source ~/.zshrc`.

## Python ML Environment (GPU‑ready)
- Location: `~/.venvs/ml-mps` (Python 3.12)
- Activate: `source ~/.venvs/ml-mps/bin/activate`
- Installed (core): Torch 2.9.0 (MPS ✅), transformers, datasets, accelerate, MLX 0.29.x, mlx-lm, coremltools
- Recommended env vars (set via `ai-env.sh` below):
  - `PYTORCH_ENABLE_MPS_FALLBACK=1`
  - `PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.0`

GPU test snippets:
```
python -c "import torch;print(torch.__version__, 'MPS:', torch.backends.mps.is_available())"
python - <<'PY'
import mlx.core as mx
(a:=mx.random.uniform((1024,1024)) @ mx.random.uniform((1024,1024))).wait()
print('MLX on', mx.default_device())
PY
```

Load env + vars in one go:
- Script: `source Desktop/AI-Cheatsheet/scripts/ai-env.sh`
- Alias: `ai-env`

## Ollama (Models + Usage)
Models on device and friendly aliases:
- `chat:latest` → `llama3.1:8b` (general assistant)
- `reason:latest` → `deepseek-r1:8b-llama-distill-q4_K_M` (small on‑device reasoning)
- `code:latest` → `qwen2.5-coder:7b` (coding + repair)
- `embed:latest` → `bge-m3:latest` (multilingual embeddings)
- Also present: `qwen2.5:7b`, `qwen2.5:14b` (optional, ~9 GB), `mxbai-embed-large:latest`

Common commands:
- Run a chat model: `ollama run chat:latest`
- Reasoning: `ollama run reason:latest`
- Coding: `ollama run code:latest`
- Embeddings: `ollama run embed:latest`
- List: `ollama list`
- Pull/update: `ollama pull llama3.1:8b`
- Remove: `ollama rm qwen2.5:14b` (free ~9 GB if unused)

Note: Some Ollama versions don’t include `ollama prune`; upgrade if you want blob pruning.

## Monitoring & Power
- Keep awake for long runs: `caffeinate -dimsu` (handled by `ai-on.sh`)
- GPU activity: Activity Monitor → View → GPU History
- Power mode: `pmset -g | grep lowpowermode` (1 = Low Power enabled)

## Known Gotchas
- Python 3.13 + MPS (Sequoia): MPS backend may be “built but unavailable”. Use Python 3.12 for Torch MPS.
- Prefer native macOS (avoid Docker) for GPU‑accelerated workloads.
- Large/old models can waste disk/memory; keep the curated set above.

## Optional: Metal Toolchain Fix (low priority)
If you need full Metal developer tools (e.g., `metallib`):
```
sudo xcode-select -s /Applications/Xcode.app
sudo xcodebuild -runFirstLaunch
# Then install "Metal Developer Tools for Xcode 26" from Apple Developer
```

## Device Context Snippet (drop into new repos)
Copy/paste the following to briefly describe your local runtime:
```
Environment (Mac M4 Max, 48 GB):
- Runtimes: Ollama (Metal), PyTorch (MPS), MLX (Apple Silicon), Core ML tools
- Python venv: ~/.venvs/ml-mps (Python 3.12) with Torch 2.9, HF stack, MLX 0.29
- Toggles: AI Mode (Desktop/AI-Cheatsheet/scripts/ai-on.sh), Chill Mode (ai-off.sh)
- Models (Ollama aliases):
  * chat: llama3.1:8b (general)
  * reason: deepseek-r1:8b-llama-distill-q4_K_M (reasoning)
  * code: qwen2.5-coder:7b (coding)
  * embed: bge-m3 (embeddings)
- Notes: Use Python 3.12 for Torch MPS; avoid Docker for GPU workloads on macOS.
```

---
Questions or updates? Adjust scripts in `Desktop/AI-Cheatsheet/scripts/` and update this README as needed.
