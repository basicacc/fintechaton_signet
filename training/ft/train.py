"""Fine-tune the compliance sentinel: Qwen3.5-2B, bf16 LoRA (Unsloth).

Run under the thermal guard:
    uv run python ../thermal_guard.py -- uv run python train.py --data ../data --out out
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

os.environ.setdefault("UNSLOTH_RETURN_LOGITS", "0")

from unsloth import FastLanguageModel  # noqa: E402  (must be imported before transformers)
from unsloth.chat_templates import train_on_responses_only  # noqa: E402
from datasets import Dataset  # noqa: E402
from trl import SFTConfig, SFTTrainer  # noqa: E402
from transformers import TrainerCallback  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402


def gpu_temp() -> int:
    out = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout.strip()
    return int(out or 0)


class ThermalThrottle(TrainerCallback):
    """Software power limit for laptop GPUs: rest between steps, and wait when hot.

    A laptop RTX 4060 goes from ~60°C to 85°C in seconds at full load; resting
    ``duty_rest`` × step time after each step keeps the average load (and heat) down.
    """

    def __init__(self, hot: int = 72, cool: int = 66, duty_rest: float = 0.5):
        self.hot, self.cool, self.duty_rest = hot, cool, duty_rest
        self.t0 = time.perf_counter()
        self.waited = 0.0
        self.peak = 0

    def on_step_begin(self, args, state, control, **kw):
        self.t0 = time.perf_counter()

    def on_step_end(self, args, state, control, **kw):
        step = time.perf_counter() - self.t0
        time.sleep(step * self.duty_rest)
        t = gpu_temp()
        self.peak = max(self.peak, t)
        if t >= self.hot:
            start = time.perf_counter()
            while gpu_temp() > self.cool:
                time.sleep(1)
            self.waited += time.perf_counter() - start
            print(f"[throttle] step {state.global_step}: {t}°C, cooled for {time.perf_counter() - start:.0f}s",
                  flush=True)


def load(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="Qwen/Qwen3.5-2B")
    ap.add_argument("--data", default="../data")
    ap.add_argument("--out", default="out")
    ap.add_argument("--epochs", type=float, default=2)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--accum", type=int, default=2)
    ap.add_argument("--max-len", type=int, default=384)
    ap.add_argument("--gguf", default="q8_0", help="'' to skip GGUF export")
    a = ap.parse_args()
    out = Path(a.out)

    model, tok = FastLanguageModel.from_pretrained(model_name=a.base, max_seq_length=a.max_len,
                                                   load_in_4bit=False, load_in_16bit=True)
    model = FastLanguageModel.get_peft_model(
        model, r=16, lora_alpha=16, lora_dropout=0, bias="none", random_state=3407,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        use_gradient_checkpointing="unsloth", max_seq_length=a.max_len)

    text_tok = getattr(tok, "tokenizer", tok)  # vision-language processors wrap the tokenizer

    def render(rows: list[dict]) -> Dataset:
        texts = [text_tok.apply_chat_template(r["messages"], tokenize=False, enable_thinking=False) for r in rows]
        return Dataset.from_dict({"text": texts})

    train_ds = render(load(Path(a.data) / "train.jsonl"))
    print("example:\n", train_ds[0]["text"])

    trainer = SFTTrainer(
        model=model, tokenizer=text_tok, train_dataset=train_ds,
        args=SFTConfig(
            dataset_text_field="text", max_length=a.max_len, per_device_train_batch_size=a.batch,
            gradient_accumulation_steps=a.accum, num_train_epochs=a.epochs, learning_rate=a.lr,
            lr_scheduler_type="cosine", warmup_ratio=0.05, weight_decay=0.01, logging_steps=10,
            optim="adamw_8bit", bf16=True, output_dir=str(out / "ckpt"), save_strategy="no",
            report_to="none", seed=3407),
    )
    # Learn only the JSON answer, not the prompt.
    trainer = train_on_responses_only(trainer, instruction_part="<|im_start|>user\n",
                                      response_part="<|im_start|>assistant\n")
    throttle = ThermalThrottle()
    trainer.add_callback(throttle)
    stats = trainer.train()
    (out / "train_stats.json").write_text(json.dumps(
        {**stats.metrics, "peak_gpu_c": throttle.peak, "thermal_wait_s": round(throttle.waited)}, indent=2))

    model.save_pretrained(str(out / "lora"))
    text_tok.save_pretrained(str(out / "lora"))
    if a.gguf:
        model.save_pretrained_gguf(str(out / "gguf"), text_tok, quantization_method=a.gguf)
    print("done", stats.metrics)


if __name__ == "__main__":
    main()
