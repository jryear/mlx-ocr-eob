# Glossary (AI/ML on Apple Silicon)

- MPS (Metal Performance Shaders)
  - Apple’s GPU backend for PyTorch on macOS. Enables training/inference on the built‑in GPU via Metal. Prefer Python 3.12 for compatibility.

- MLX
  - Apple’s lightweight array/NN library optimized for Apple Silicon with memory‑mapping and mixed precision. Great for on‑device LLM inference and LoRA.

- Core ML / Neural Engine
  - Apple’s model format/runtime for apps. You can run models on CPU/GPU/Neural Engine via `.computeUnits = .all`. Convert with `coremltools`.

- Ollama
  - Local LLM runtime with Metal acceleration. Pull models (GGUF) and run via `ollama run <model:tag>`. You created friendly aliases (chat/code/reason/embed).

- Quantization (e.g., q4_K_M)
  - Lower‑bit weight compression to reduce memory/latency. `q4_K_M` is a popular 4‑bit K‑quant variant for GGUF that balances quality/speed.

- GGUF
  - File format used by llama.cpp/Ollama for quantized models. Layers are stored as blobs; Ollama stores these under `~/.ollama/models/blobs/`.

- High/Low Power Mode
  - macOS power modes. Disable Low Power for best performance. Scripts here toggle it as part of AI‑on/off.

- caffeinate
  - Utility to prevent sleep during long jobs. `ai-on.sh` starts it, `ai-off.sh` stops it.

- Thompson Sampling / Orchestrator
  - Strategy (used by your agents) to route prompts across multiple models based on reward/latency tradeoffs. Benefits from having multiple capable models ready (e.g., llama3.1, qwen2.5, deepseek‑r1).

