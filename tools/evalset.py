"""Labelled evaluation set for the per-item scorer.

Prompt changes cannot be judged from aggregate output - swapping one bias for
another looks like progress. This grades a candidate prompt against items whose
correct answer is known, so each variant gets a number.

    python -m tools.evalset sample --n 200        # writes eval/items.jsonl
    # label the file (see below), then:
    python -m tools.evalset grade --labels eval/items.jsonl --prompt theta

Labelling
---------
Each line is one JSON object. Fill in the three `label_*` fields:

  label_relevant   0/1   would an index trader act on this at all?
  label_direction  -1/0/+1  effect on SPX over ~3 weeks (0 if not relevant)
  label_risk       0/1   does it raise the chance of a LARGE adverse move?

`label_risk` is the one that matters most for a credit-spread book: those
positions survive drift and die on shocks, so a scorer that misses a genuine
risk item is far more expensive than one that miscalls direction.

Leave `label_relevant` as null to skip an item.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict

from analysis.llm import OllamaClient
from analysis.prompts import PROMPTS, get_prompt
from config import get_settings
from db.models import Item
from db.repository import Repository
from utils.logging import setup_logging

NEUTRAL_BAND = 0.05


# ------------------------------------------------------------------ sample --
def cmd_sample(args) -> None:
    settings = get_settings()
    repo = Repository(settings.database_url)
    with repo.session() as session:
        rows = session.query(Item).filter(Item.scored.is_(True)).limit(args.pool).all()

    by_source = defaultdict(list)
    for item in rows:
        by_source[item.source_type].append(item)

    # Even-ish coverage per source, so a rare-but-important type (econ) is not
    # swamped by social volume the way the live corpus is.
    random.seed(args.seed)
    per = max(1, args.n // max(1, len(by_source)))
    picked = []
    for source_type, items in sorted(by_source.items()):
        picked.extend(random.sample(items, min(per, len(items))))
    remainder = [i for i in rows if i not in picked]
    if len(picked) < args.n and remainder:
        picked.extend(random.sample(remainder, min(args.n - len(picked), len(remainder))))

    chosen = picked[: args.n]
    # Written directly rather than via stdout redirection: these items contain
    # smart quotes and dashes, and the default Windows console encoding
    # (cp1252) cannot represent them.
    with open(args.out, "w", encoding="utf-8") as handle:
        for item in chosen:
            handle.write(json.dumps({
                "id": item.id,
                "source_type": item.source_type,
                "category": item.category,
                "title": item.title,
                "text": (item.content or "")[:1200],
                "label_relevant": None,
                "label_direction": None,
                "label_risk": None,
            }, ensure_ascii=False) + "\n")

    counts = Counter(i.source_type for i in chosen)
    print(f"wrote {len(chosen)} items to {args.out}")
    print(f"by source: {dict(counts)}")
    print(f"\nNext: fill in label_relevant / label_direction / label_risk on each line,")
    print(f"then: python -m tools.evalset grade --labels {args.out}")


# ------------------------------------------------------------------- grade --
def _bucket(value: float) -> int:
    if value < -NEUTRAL_BAND:
        return -1
    if value > NEUTRAL_BAND:
        return 1
    return 0


def cmd_grade(args) -> None:
    labelled = []
    with open(args.labels, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            row = json.loads(line)
            if row.get("label_relevant") is not None:
                labelled.append(row)

    if not labelled:
        sys.exit(f"No labelled rows in {args.labels} - fill in the label_* fields first.")

    settings = get_settings()
    log = setup_logging("WARNING", None)
    llm = OllamaClient(settings.ollama_base_url, settings.ollama_model, log)
    names = args.prompt or list(PROMPTS)

    print(f"grading {len(labelled)} labelled items against: {', '.join(names)}")
    print(f"model: {settings.ollama_model}\n")

    for name in names:
        system, template = get_prompt(name)
        preds = []
        for row in labelled:
            out = llm.generate_json(
                template.format(
                    stype=row.get("source_type", ""),
                    category=row.get("category") or "",
                    title=row.get("title") or "",
                    text=row.get("text") or "",
                ),
                system=system,
            )
            if not out:
                preds.append(None)
                continue
            try:
                direction = max(-1.0, min(1.0, float(out.get("direction"))))
                magnitude = max(0.0, min(1.0, float(out.get("magnitude", 0))))
            except (TypeError, ValueError):
                preds.append(None)
                continue
            preds.append((direction, magnitude))

        _report(name, labelled, preds)


def _report(name: str, labelled: list[dict], preds: list) -> None:
    pairs = [(r, p) for r, p in zip(labelled, preds) if p is not None]
    if not pairs:
        print(f"\n{name}: every call failed to parse\n")
        return

    # Relevance: does the scorer separate signal from noise at all?
    tp = sum(1 for r, p in pairs if r["label_relevant"] and _bucket(p[0]) != 0)
    fn = sum(1 for r, p in pairs if r["label_relevant"] and _bucket(p[0]) == 0)
    fp = sum(1 for r, p in pairs if not r["label_relevant"] and _bucket(p[0]) != 0)
    tn = sum(1 for r, p in pairs if not r["label_relevant"] and _bucket(p[0]) == 0)

    relevant = [(r, p) for r, p in pairs if r["label_relevant"]]
    dir_hits = sum(1 for r, p in relevant if _bucket(p[0]) == r["label_direction"])

    # Risk recall: the expensive error for a premium seller is missing a shock.
    risky = [(r, p) for r, p in pairs if r.get("label_risk")]
    risk_caught = sum(1 for r, p in risky if abs(p[0]) > 0.3 or p[1] > 0.5)

    scored = [p[0] for _, p in pairs]
    bear = sum(1 for d in scored if d < -NEUTRAL_BAND)
    bull = sum(1 for d in scored if d > NEUTRAL_BAND)

    def rate(hit, total):
        return f"{hit / total * 100:5.1f}%  ({hit}/{total})" if total else "    n/a"

    print(f"\n{'=' * 58}\n{name}\n{'=' * 58}")
    print(f"  parsed              {len(pairs)}/{len(labelled)}")
    print(f"  relevance precision {rate(tp, tp + fp)}   <- of what it flags, how much matters")
    print(f"  relevance recall    {rate(tp, tp + fn)}   <- of what matters, how much it flags")
    print(f"  direction accuracy  {rate(dir_hits, len(relevant))}   (on relevant items only)")
    print(f"  RISK recall         {rate(risk_caught, len(risky))}   <- misses here cost real money")
    print(f"  bear:bull ratio     {bear}:{bull}"
          f"   ({bear / max(1, bull):.1f} : 1)   labelled {sum(1 for r,_ in pairs if r['label_direction'] == -1)}"
          f":{sum(1 for r,_ in pairs if r['label_direction'] == 1)}")
    print(f"  true negatives      {tn}  (correctly ignored noise)")


# ------------------------------------------------------------------- label --
_HELP = """
  r / relevant?   would an index trader act on this at all?
  d / direction   effect on SPX over ~3 weeks
  k / risk        does it raise the chance of a LARGE adverse move?

keys:  n = not relevant (sets direction 0, risk 0, and moves on)
       b = bearish     u = bullish      f = flat but relevant
  then risk:  y = raises shock risk     enter = no
  s = skip   q = save and quit
"""


def cmd_label(args) -> None:
    """Label items one at a time. Far quicker than hand-editing JSON."""
    path = args.labels
    with open(path, encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]

    todo = [i for i, r in enumerate(rows) if r.get("label_relevant") is None]
    print(f"{len(rows)} items, {len(todo)} unlabelled")
    print(_HELP)

    def save():
        with open(path, "w", encoding="utf-8") as out:
            for r in rows:
                out.write(json.dumps(r, ensure_ascii=False) + "\n")

    for count, idx in enumerate(todo, 1):
        row = rows[idx]
        print("\n" + "-" * 70)
        print(f"[{count}/{len(todo)}]  {row['source_type']} / {row.get('category') or '-'}")
        if row.get("title"):
            print(f"TITLE: {row['title']}")
        text = (row.get("text") or "").strip().replace("\n", " ")
        print(f"TEXT : {text[:400]}{'...' if len(text) > 400 else ''}")

        choice = input("  [n]ot relevant / [b]earish / [u]p / [f]lat / [s]kip / [q]uit > ").strip().lower()
        if choice == "q":
            save()
            print(f"saved. {sum(1 for r in rows if r.get('label_relevant') is not None)} labelled.")
            return
        if choice == "s":
            continue
        if choice == "n":
            row.update(label_relevant=0, label_direction=0, label_risk=0)
        elif choice in ("b", "u", "f"):
            row["label_relevant"] = 1
            row["label_direction"] = {"b": -1, "u": 1, "f": 0}[choice]
            risk = input("  raises risk of a LARGE move? [y/Enter] > ").strip().lower()
            row["label_risk"] = 1 if risk == "y" else 0
        else:
            print("  (unrecognised, skipping)")
            continue
        if count % 10 == 0:
            save()
            print(f"  ...saved ({count} done)")

    save()
    print(f"\ndone. {sum(1 for r in rows if r.get('label_relevant') is not None)} labelled.")
    print(f"next: python -m tools.evalset grade --labels {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sample", help="write unlabelled items as JSONL")
    s.add_argument("--n", type=int, default=200)
    s.add_argument("--out", default="eval/items.jsonl")
    s.add_argument("--pool", type=int, default=6000, help="rows to draw the sample from")
    s.add_argument("--seed", type=int, default=42)
    s.set_defaults(func=cmd_sample)

    lb = sub.add_parser("label", help="label items interactively")
    lb.add_argument("--labels", default="eval/items.jsonl")
    lb.set_defaults(func=cmd_label)

    g = sub.add_parser("grade", help="score prompts against labelled items")
    g.add_argument("--labels", required=True)
    g.add_argument("--prompt", action="append",
                   help=f"repeatable; one of {list(PROMPTS)} (default: all)")
    g.set_defaults(func=cmd_grade)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
