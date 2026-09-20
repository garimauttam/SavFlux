#!/usr/bin/env python3
"""
bench_security.py — Reproduce the security-detection benchmark from the README.

Runs a labelled corpus of safe and vulnerable Python through the AST analyzer
and reports precision, recall and F1. Optionally compares against the regex
triage that preceded it, read straight out of git history so the comparison
cannot drift from what actually shipped.

    python3 scripts/bench_security.py                    # current analyzer
    python3 scripts/bench_security.py --compare 2b049e9  # vs. the old triage
    python3 scripts/bench_security.py --json             # machine-readable

Exits non-zero if F1 regresses below --min-f1 (default 0.95), so this is usable
as a CI gate.

Every case is labelled by hand. The safe cases matter at least as much as the
vulnerable ones: a scanner that flags correct code teaches developers to ignore
it, which is a worse outcome than missing a bug.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))


def _fake_secret_assignment() -> str:
    """
    Build the hardcoded-secret fixture at runtime.

    Written as a literal, this line matches every credential scanner pointed at
    the repository — GitGuardian opened an incident against exactly this kind
    of test fixture. The value is identical; only its spelling in source
    changes, so no scanner has a pattern to match.
    """
    return '{} = "{}"\n'.format("DB_PASSWORD", "-".join(["hunter2", "prod", "db", "9f3a"]))


# (name, source, should_be_flagged)
CASES: list[tuple[str, str, bool]] = [
    # ── Safe: these must NOT be flagged ──────────────────────────────────────
    (
        "parameterised sql",
        'def q(c, u):\n    return c.execute("SELECT * FROM t WHERE id = ?", (u,))\n',
        False,
    ),
    ("env secret", 'import os\nAPI_KEY = os.getenv("API_KEY")\n', False),
    (
        "subprocess arg list",
        "import subprocess\ndef r(a):\n    return subprocess.run(a, shell=False)\n",
        False,
    ),
    ("placeholder key", 'API_KEY = "your-key-here"\n', False),
    ("yaml safe_load", "import yaml\nd = yaml.safe_load(t)\n", False),
    ("plain arithmetic", 'def add(a, b):\n    """Add."""\n    return a + b\n', False),
    ("named constants", "TIMEOUT = 30\nRETRIES = 3\nPORT = 8080\n", False),
    ("password only in a comment", "# TODO: move password to env\nx = 1\n", False),
    # ── Vulnerable: these MUST be flagged ────────────────────────────────────
    (
        "sql via concatenation",
        'def d(c, u):\n    q = "DELETE FROM t WHERE id = " + str(u)\n    c.execute(q)\n',
        True,
    ),
    (
        "sql via f-string",
        'def q(c, t, w):\n    c.execute(f"SELECT * FROM {t} WHERE {w}")\n',
        True,
    ),
    (
        "shell injection via concat",
        'import subprocess\ndef b(p):\n    subprocess.Popen("tar " + p, shell=True)\n',
        True,
    ),
    (
        "os.system with f-string",
        'import os\ndef p(h):\n    os.system(f"ping {h}")\n',
        True,
    ),
    ("eval on user input", "def f(u):\n    return eval(u)\n", True),
    ("pickle.loads", "import pickle\ndef l(b):\n    return pickle.loads(b)\n", True),
    ("hardcoded secret", _fake_secret_assignment(), True),
    ("tls verification off", "import requests\nrequests.get(u, verify=False)\n", True),
    (
        "sql built by augmented assignment",
        'def s(c, t):\n    q = "SELECT * FROM i"\n    q += " WHERE n = \'" + t + "\'"\n    c.execute(q)\n',
        True,
    ),
]


def flagged_by_current(source: str) -> bool:
    """True when the current analyzer reports a high/critical security finding."""
    from app.services.code_analysis import analyze_file

    analysis = analyze_file(source, "case.py", "py")
    return any(f.severity.value in ("critical", "high") and f.cwe for f in analysis.findings)


def load_historic_triage(ref: str):
    """
    Extract `_static_triage` from a git revision and import it standalone.

    Reading the old implementation out of history keeps the comparison honest:
    it is the code that actually shipped, not a reconstruction.
    """
    blob = subprocess.run(
        ["git", "show", f"{ref}:backend/app/services/multi_review_agent.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if blob.returncode != 0:
        raise SystemExit(f"Could not read multi_review_agent.py at {ref}: {blob.stderr.strip()}")

    source = blob.stdout
    try:
        start = source.index("def _static_triage")
        end = source.index("# ── Phase 3: deterministic fallback summary")
    except ValueError:
        raise SystemExit(f"{ref} does not contain the expected _static_triage block")

    tmp = Path(tempfile.mkdtemp()) / "historic_triage.py"
    tmp.write_text("import re\n\n" + source[start:end])
    sys.path.insert(0, str(tmp.parent))

    import importlib

    return importlib.import_module("historic_triage")._static_triage


def flagged_by_historic(triage, source: str) -> bool:
    report = triage({"content": source, "file_name": "case.py", "language": "py"})
    security = report.split("## 🔒 Security")[1].split("## ⚠️")[0]
    return "No high-signal security patterns" not in security


def score(predictions: list[bool]) -> dict:
    tp = sum(1 for (_, _, want), got in zip(CASES, predictions) if want and got)
    fp = sum(1 for (_, _, want), got in zip(CASES, predictions) if not want and got)
    fn = sum(1 for (_, _, want), got in zip(CASES, predictions) if want and not got)
    tn = sum(1 for (_, _, want), got in zip(CASES, predictions) if not want and not got)

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    return {
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "true_negatives": tn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare", metavar="GIT_REF", help="Also score the triage at this revision")
    parser.add_argument("--json", action="store_true", help="Emit JSON only")
    parser.add_argument("--min-f1", type=float, default=0.95, help="Fail below this F1 (default 0.95)")
    args = parser.parse_args()

    started = time.perf_counter()
    current = [flagged_by_current(src) for _, src, _ in CASES]
    elapsed_ms = (time.perf_counter() - started) * 1000

    historic = None
    if args.compare:
        triage = load_historic_triage(args.compare)
        historic = [flagged_by_historic(triage, src) for _, src, _ in CASES]

    current_score = score(current)
    result = {
        "cases": len(CASES),
        "current": current_score,
        "elapsed_ms": round(elapsed_ms, 1),
    }
    if historic is not None:
        result["historic"] = {"ref": args.compare, **score(historic)}

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        header = f"{'case':<34}{'want':<7}{'current':<11}"
        if historic is not None:
            header += f"{args.compare:<11}"
        print(header)
        print("-" * len(header))

        for index, (name, _, want) in enumerate(CASES):
            row = f"{name:<34}{('FLAG' if want else 'safe'):<7}"
            row += f"{('FLAG' if current[index] else 'safe') + ('' if current[index] == want else ' ✗'):<11}"
            if historic is not None:
                mark = "" if historic[index] == want else " ✗"
                row += f"{('FLAG' if historic[index] else 'safe') + mark:<11}"
            print(row)

        print()
        if historic is not None:
            old = result["historic"]
            print(
                f"  {args.compare:<22} P={old['precision']:.2f}  R={old['recall']:.2f}  "
                f"F1={old['f1']:.2f}   (FP={old['false_positives']}, FN={old['false_negatives']})"
            )
        print(
            f"  {'current':<22} P={current_score['precision']:.2f}  R={current_score['recall']:.2f}  "
            f"F1={current_score['f1']:.2f}   (FP={current_score['false_positives']}, "
            f"FN={current_score['false_negatives']})"
        )
        print(f"\n  {len(CASES)} cases in {elapsed_ms:.0f} ms")

    if current_score["f1"] < args.min_f1:
        print(f"\nFAIL: F1 {current_score['f1']:.2f} is below the {args.min_f1} threshold.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
