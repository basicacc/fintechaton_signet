# Compliance sentinel: data, fine-tuning, evaluation

A Qwen3.5-2B model fine-tuned (bf16 LoRA) for two narrow classification tasks used by the advisor:

- **`client_signals`**: what a client says → `loss_averse`, `dependent_funds`, `short_horizon`, `life_change`. Feeds the profile-integrity check.
- **`advisor_claims`**: what the AI advisor writes → `guarantee`, `deposit_confusion`, `exaggeration`, `pressure`. Adds to the deterministic speech checks, catching paraphrases that patterns miss.

The sentinel only ever **adds** findings. The deterministic rules run regardless, so a sentinel error can't loosen a check.

## 1. Data (`make_data.py`, `seeds.py`)

- 100 hand-written **training seeds** (10 per label, about 30% Chinese), each paraphrased 8× by the local `qwen3.5:9b` model, plus 500 multi-label combinations: **1,400 training examples**.
- 40 separately written **test seeds**, in different words and situations, plus 80 combinations: **120 test examples**. Test seeds are never paraphrased into training data, and any exact overlap is removed.

```bash
uv run python make_data.py --paraphrases 8 --out data   # needs Ollama with qwen3.5:9b
```

## 2. Fine-tuning (`ft/train.py`)

Unsloth, bf16 LoRA (r=16, all attention and MLP projections), 2 epochs, loss on the assistant answer only, thinking disabled, about 5 GB VRAM. Exported to GGUF and quantized to q4_K_M (1.3 GB), so it stays loaded next to the 9B advisor on an 8 GB GPU.

```bash
cd ft && uv sync
ollama stop qwen3.5:9b        # free the GPU first (8 GB cards can't hold both while training)
OMP_NUM_THREADS=4 uv run python ../thermal_guard.py -- uv run python train.py --data ../data --out out
ollama create pca-sentinel -f Modelfile
```

To export q4_K_M (what we serve): convert the merged model with llama.cpp's `convert_hf_to_gguf.py --outtype f16`, build `llama-quantize` (CPU only: `cmake -B build-cpu -DGGML_CUDA=OFF && cmake --build build-cpu --target llama-quantize -j2`, under `thermal_guard.py`), run `llama-quantize <f16.gguf> pca-sentinel-q4_k_m.gguf Q4_K_M 2`, and point the Modelfile at it. Evaluation results for both quantizations are in `results/`.

`thermal_guard.py` pauses the whole job when the GPU reaches 80°C or the CPU reaches 92°C, resumes once cool, and aborts at 85°C GPU / 99°C CPU.

## 3. Evaluation (`eval_sentinel.py`)

```bash
cd ../backend && uv run python ../training/eval_sentinel.py --models qwen3.5:9b qwen3.5:2b pca-sentinel
```

It compares the regex layer, the base 9B and 2B (zero-shot, same prompt) and the fine-tuned sentinel on the held-out test set: exact match, micro precision/recall/F1, per-label scores, and latency. Results are written to `results/sentinel_eval.json` and summarised in the main README.
