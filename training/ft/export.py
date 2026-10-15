"""Merge the sentinel LoRA into the base model and write 16-bit safetensors.

    uv run python ../thermal_guard.py -- uv run python export.py --lora out/lora --out out/merged
    ollama create pca-sentinel -f Modelfile   # FROM ./out/merged, quantised by Ollama
Pass --gguf q8_0 to export GGUF via llama.cpp instead (slower: builds llama.cpp).
"""

from __future__ import annotations

import argparse

from unsloth import FastLanguageModel


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lora", default="out/lora")
    ap.add_argument("--out", default="out/merged")
    ap.add_argument("--gguf", default="")
    a = ap.parse_args()
    model, tok = FastLanguageModel.from_pretrained(model_name=a.lora, max_seq_length=512,
                                                   load_in_4bit=False, load_in_16bit=True)
    text_tok = getattr(tok, "tokenizer", tok)
    if a.gguf:
        model.save_pretrained_gguf(a.out, text_tok, quantization_method=a.gguf)
    else:
        model.save_pretrained_merged(a.out, text_tok, save_method="merged_16bit")
    print("exported to", a.out)


if __name__ == "__main__":
    main()
