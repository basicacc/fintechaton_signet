# Third-party components

Licences were read from each package's installed metadata or official model card on 2026-10-14. All components are used unmodified, under their own licences.

## Models

| Component | Version / tag | Licence | Use |
|---|---|---|---|
| Qwen3.5-9B (Alibaba Qwen team) | Ollama `qwen3.5:9b` | Apache-2.0 | Advisor model (local inference) |
| Qwen3.5-2B (Alibaba Qwen team) | HF `Qwen/Qwen3.5-2B` | Apache-2.0 | Base of the fine-tuned compliance sentinel; baseline in evaluation |

The fine-tuned sentinel is a LoRA derivative of Qwen3.5-2B and is distributed under Apache-2.0, with the original licence and notice preserved.

## Runtime and infrastructure

| Component | Licence | Use |
|---|---|---|
| FISCO BCOS 3.17.1 (WeBank and community) | Apache-2.0 | Consortium chain for certificate anchoring (downloaded at setup, not vendored) |
| Ollama | MIT | Local model serving |
| Z3 (`z3-solver`) | MIT | SMT solving / optimisation |
| pydantic | MIT | Data models |
| FastAPI | MIT | HTTP API |
| Starlette | BSD-3-Clause | ASGI framework (via FastAPI) |
| uvicorn | BSD-3-Clause | ASGI server |
| httpx | BSD-3-Clause | HTTP client |
| cryptography | Apache-2.0 OR BSD-3-Clause | Ed25519 signatures |
| web3.py, eth-account, eth-utils | MIT | EVM / Web3 RPC client |
| py-solc-x | MIT | Solidity compiler installer |
| solc 0.8.26 (Solidity compiler) | GPL-3.0 | Build tool only; it compiles our own contract and isn't distributed with this project |

## Web UI

| Component | Licence |
|---|---|
| React, React DOM | MIT |
| Vite, @vitejs/plugin-react | MIT |
| TypeScript | Apache-2.0 |

The UI loads no external fonts, scripts or stylesheets.

## Development, testing and training (not part of the runtime)

| Component | Licence |
|---|---|
| pytest | MIT |
| Hypothesis | MPL-2.0 |
| eth-tester (py-evm) | MIT |
| Unsloth | Apache-2.0 |
| unsloth_zoo | LGPL-3.0-or-later (used as a library, unmodified) |
| Hugging Face transformers, datasets, PEFT, TRL | Apache-2.0 |
| PyTorch | BSD-3-Clause style / Apache-2.0 components |
| xformers | BSD |
| llama.cpp (`convert_hf_to_gguf.py`, `llama-quantize`, used to export the sentinel) | MIT |

## Data

- Regulatory text is quoted from official public sources (NFRA Order No. 7 of 2025; HK Government; SFC). The articles each rule implements are cited in `backend/src/pca/rules.py` and listed in the README.
- The product catalogue and all client personas are **fictional**. Return figures are illustrative, not real performance data.
- Sentinel seed sentences were written for this project and paraphrased by the local Qwen3.5-9B model; no third-party datasets were used.
