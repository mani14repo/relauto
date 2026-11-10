"""Can a non-generative model match checklist items that keyword patterns miss, without false matches?

    python eval/check_matchers.py                       # keywords and embeddings (bge-small)
    python eval/check_matchers.py --cross-encoder Xenova/ms-marco-MiniLM-L-6-v2

Test set: eval/checklist_matching.yaml (reworded lines that satisfy an item, near misses that do not) plus every
line of the eval scenario documents as unrelated background. A matcher scores each (item, line) pair; at a
threshold an item counts as found in a line when the score reaches it. What matters:
  found      share of satisfying lines matched (recall; keywords miss these by design)
  near miss  share of near-miss lines wrongly matched (these would become false Mets)
  bg items   items for which any background line matched (also false Mets)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sra.config import load_yaml  # noqa: E402
from sra.detectors.checklists import _lines  # noqa: E402

# which configured checklist item each test item corresponds to (for the keyword baseline)
KEYWORD_ITEM = {
    "boundaries": "boundary conditions", "disable": "disable procedure", "escalation": "escalation path",
    "segmentation": "human vs automated segmentation", "check-recorded": "check recorded for the latest release",
    "authority": "named authority", "user-impact": "user impact scoped", "owner-sign-off": "business owner sign-off",
    "raci-confirmed": "confirmed by PSE, SRE and engineering leads", "response-commitment": "response commitment per priority",
    "reduction-target": "reduction target of 20% or more", "upgrade-path": "upgrade path",
}


def load():
    items = yaml.safe_load((ROOT / "eval" / "checklist_matching.yaml").read_text(encoding="utf-8"))["items"]
    bg = sorted({ln for p in (ROOT / "eval" / "scenarios").glob("*/evidence/*.md") for ln in _lines(p.read_text(encoding="utf-8"))})
    return items, bg


def keyword_scores(item: dict, lines: list[str]) -> np.ndarray:
    cid, slug = item["id"].split("/")
    spec = load_yaml("checklists")["criteria"][cid]
    it = next(i for i in spec["items"] if i["name"] == KEYWORD_ITEM[slug])
    def hit(ln):
        if any(re.search(p, ln, re.I | re.M) for p in it.get("none", [])):
            return 0.0
        return 1.0 if any(re.search(p, ln, re.I | re.M) for p in it.get("any", [])) else 0.0
    return np.array([hit(ln) for ln in lines])


class Embeddings:
    name = "embeddings (bge-small, cosine)"

    def __init__(self):
        from sra.retrieval.chunks import shared_embedder
        self.e = shared_embedder()

    def scores(self, query: str, lines: list[str]) -> np.ndarray:
        q = np.asarray(self.e.embed([query])[0])
        m = np.asarray(self.e.embed(lines))
        return m @ q / (np.linalg.norm(m, axis=1) * np.linalg.norm(q))


class CrossEncoder:
    def __init__(self, model: str):
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        self.name = f"cross-encoder ({model})"
        self.m = TextCrossEncoder(model_name=model)

    def scores(self, query: str, lines: list[str]) -> np.ndarray:
        return np.asarray(list(self.m.rerank(query, lines)), dtype=float)


class NLI:
    """Natural-language inference: does the document line (premise) entail the requirement (hypothesis)?

    Runs an ONNX export with onnxruntime + tokenizers (no PyTorch). Score = probability of "entailment".
    The requirement is phrased as a statement with a fixed template, the same for every item.
    """
    TEMPLATES = {"states": "The text states {}.", "describes": "This describes {}."}

    def __init__(self, repo: str, onnx_file: str, template: str):
        import json as _json

        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer
        self.name = f"NLI ({repo}, '{self.TEMPLATES[template]}')"
        self.template = self.TEMPLATES[template]
        self.tok = Tokenizer.from_file(hf_hub_download(repo, "tokenizer.json"))
        self.tok.enable_truncation(256)
        self.tok.enable_padding()
        labels = _json.load(open(hf_hub_download(repo, "config.json")))["id2label"]
        self.entail = int(next(k for k, v in labels.items() if v.lower() == "entailment"))
        self.sess = ort.InferenceSession(hf_hub_download(repo, onnx_file), providers=["CPUExecutionProvider"])

    def scores(self, means: str, lines: list[str], batch: int = 64) -> np.ndarray:
        hyp = self.template.format(means)
        out = []
        for i in range(0, len(lines), batch):
            enc = self.tok.encode_batch([(ln, hyp) for ln in lines[i:i + batch]])
            feeds = {"input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                     "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}
            logits = self.sess.run(None, feeds)[0]
            p = np.exp(logits - logits.max(axis=1, keepdims=True))
            out.append((p / p.sum(axis=1, keepdims=True))[:, self.entail])
        return np.concatenate(out)


def evaluate(name, score_fn, items, bg, thresholds):
    pos, near, bgmax = [], [], []
    for it in items:
        s = score_fn(it, it["satisfies"] + it["near_miss"] + bg)
        n1, n2 = len(it["satisfies"]), len(it["near_miss"])
        pos += list(s[:n1])
        near += list(s[n1:n1 + n2])
        bgmax.append(float(np.max(s[n1 + n2:])))
    pos, near, bgmax = map(np.asarray, (pos, near, bgmax))
    print(f"\n== {name}")
    print(f"   score ranges: satisfying {pos.min():.2f}..{pos.max():.2f}; near misses {near.min():.2f}..{near.max():.2f}; "
          f"best background line per item {bgmax.min():.2f}..{bgmax.max():.2f}")
    print("   threshold | found      | near miss accepted | items with a background match")
    for t in thresholds:
        print(f"   {t:9.2f} | {np.mean(pos >= t):5.0%} {int(np.sum(pos >= t)):2d}/{len(pos)} | {np.mean(near >= t):5.0%} "
              f"{int(np.sum(near >= t)):2d}/{len(near)}         | {int(np.sum(bgmax >= t)):2d}/{len(bgmax)}")
    return pos, near, bgmax


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cross-encoder", default="")
    ap.add_argument("--nli", default="", help="Hugging Face repo of an ONNX NLI model, e.g. cross-encoder/nli-deberta-v3-xsmall")
    ap.add_argument("--nli-file", default="onnx/model_quint8_avx2.onnx")
    ap.add_argument("--skip-baselines", action="store_true")
    args = ap.parse_args()
    items, bg = load()
    print(f"{len(items)} items, {sum(len(i['satisfies']) for i in items)} satisfying lines, "
          f"{sum(len(i['near_miss']) for i in items)} near misses, {len(bg)} background lines")
    evaluate("keywords (current checklist patterns)", lambda it, lines: keyword_scores(it, lines), items, bg, [1.0])
    if not args.skip_baselines:
        emb = Embeddings()
        evaluate(emb.name, lambda it, lines: emb.scores(it["means"], lines), items, bg, [0.6, 0.65, 0.7, 0.75, 0.8])
    if args.nli:
        for template in NLI.TEMPLATES:
            nli = NLI(args.nli, args.nli_file, template)
            evaluate(nli.name, lambda it, lines: nli.scores(it["means"], lines), items, bg, [0.5, 0.7, 0.8, 0.9, 0.95, 0.98, 0.99])
    if args.cross_encoder:
        ce = CrossEncoder(args.cross_encoder)
        pos, near, bgmax = evaluate(ce.name, lambda it, lines: ce.scores(it["means"], lines), items, bg, [])
        lo = float(np.floor(min(pos.min(), near.min()))); hi = float(np.ceil(max(pos.max(), bgmax.max())))
        evaluate(ce.name, lambda it, lines: ce.scores(it["means"], lines), items, bg, list(np.linspace(lo, hi, 9)))


if __name__ == "__main__":
    main()
