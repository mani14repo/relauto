"""Natural-language inference (NLI) with a small non-generative model: does a sentence entail a requirement?

Used only to SUGGEST evidence lines to a reviewer (see sra.detectors.checklists). The model is an ONNX export run
with onnxruntime + tokenizers, so no PyTorch is needed. It is loaded from the local Hugging Face cache only and is
never downloaded implicitly: put it there once (or mirror it for the cluster) with

    python -c "from huggingface_hub import hf_hub_download as d; r='cross-encoder/nli-deberta-v3-xsmall'; \
               [d(r, f) for f in ('config.json', 'tokenizer.json', 'onnx/model_quint8_avx2.onnx')]"

Measured on eval/checklist_matching.yaml with eval/check_matchers.py before being wired in.
"""
from __future__ import annotations

import json
import logging
import threading

import numpy as np

log = logging.getLogger(__name__)
_LOCK = threading.Lock()
_LOADED: dict[tuple[str, str], "NliScorer | str"] = {}     # (repo, file) -> scorer, or the reason it is unavailable


class NliScorer:
    def __init__(self, repo: str, onnx_file: str, max_tokens: int = 256):
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer
        get = lambda f: hf_hub_download(repo, f, local_files_only=True)  # noqa: E731 - never download here
        self.repo = repo
        self.tok = Tokenizer.from_file(get("tokenizer.json"))
        self.tok.enable_truncation(max_tokens)
        self.tok.enable_padding()
        labels = json.load(open(get("config.json"), encoding="utf-8"))["id2label"]
        self.entail = int(next(k for k, v in labels.items() if v.lower() == "entailment"))
        self.sess = ort.InferenceSession(get(onnx_file), providers=["CPUExecutionProvider"])

    def entailment(self, premises: list[str], hypothesis: str, batch: int = 64) -> np.ndarray:
        """Probability that each premise entails the hypothesis."""
        out = []
        for i in range(0, len(premises), batch):
            enc = self.tok.encode_batch([(p, hypothesis) for p in premises[i:i + batch]])
            feeds = {"input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                     "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}
            logits = self.sess.run(None, feeds)[0]
            p = np.exp(logits - logits.max(axis=1, keepdims=True))
            out.append((p / p.sum(axis=1, keepdims=True))[:, self.entail])
        return np.concatenate(out) if out else np.zeros(0)


def get_scorer(repo: str, onnx_file: str) -> tuple[NliScorer | None, str]:
    """(scorer, "") when the model is in the local cache, else (None, reason). Loaded once per process."""
    key = (repo, onnx_file)
    with _LOCK:
        if key not in _LOADED:
            try:
                _LOADED[key] = NliScorer(repo, onnx_file)
            except Exception as exc:  # noqa: BLE001 - missing packages or model files: suggestions are simply off
                _LOADED[key] = f"{repo} not available locally ({type(exc).__name__})"
                log.info("NLI suggestions off: %s", _LOADED[key])
        v = _LOADED[key]
    return (v, "") if isinstance(v, NliScorer) else (None, v)
