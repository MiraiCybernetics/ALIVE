"""Judge a held-out run with the local OpenAI Codex CLI (a different model family from the generator).

Run on the machine where `codex` is installed (here: Windows), stdlib only:

    python alive/eval/judge_codex.py eval_runs/heldout_<ts>.json [--parallel 3]

Reads the generation report written by `python -m alive.eval.heldout --judge none --out ...`, sends each
item's judge prompt to `codex exec` (read-only sandbox, ephemeral, JSON schema enforced), and writes
`<input>.codex.json` with per-item judgments and a summary.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile

SCHEMA = {
    "type": "object",
    "properties": {
        "understood": {"type": "integer"},
        "fits_expectation": {"type": "integer"},
        "natural": {"type": "integer"},
        "narrates_strategy": {"type": "boolean"},
        "issues": {"type": "string"},
    },
    "required": ["understood", "fits_expectation", "natural", "narrates_strategy", "issues"],
    "additionalProperties": False,
}

PREFIX = ("你是独立评审，只根据给出的材料打分，不要运行任何命令，不要读写文件。"
          "按要求只输出符合 JSON Schema 的结果。\n\n")


def find_codex() -> str:
    for name in ("codex.cmd", "codex.exe", "codex"):
        p = shutil.which(name)
        if p:
            return p
    raise SystemExit("codex CLI not found on PATH")


def judge_one(codex: str, prompt: str, workdir: str, timeout: int = 240) -> dict:
    with tempfile.TemporaryDirectory(dir=workdir) as d:
        schema_p = os.path.join(d, "schema.json")
        out_p = os.path.join(d, "out.json")
        with open(schema_p, "w", encoding="utf-8") as f:
            json.dump(SCHEMA, f)
        cmd = [codex, "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only", "-C", d,
               "--output-schema", schema_p, "-o", out_p, "-"]
        try:
            r = subprocess.run(cmd, input=(PREFIX + prompt).encode("utf-8"), stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"error": "timeout"}
        if not os.path.exists(out_p):
            return {"error": f"no output (exit {r.returncode}): {r.stdout.decode('utf-8', 'replace')[-300:]}"}
        txt = open(out_p, encoding="utf-8").read().strip()
        try:
            return json.loads(txt)
        except Exception:
            return {"error": "bad json", "raw": txt[:500]}


def main(argv=None) -> int:
    try:  # Windows consoles default to GBK; never crash on emoji
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("--parallel", type=int, default=3)
    a = ap.parse_args(argv)
    rep = json.load(open(a.report, encoding="utf-8"))
    codex = find_codex()
    workdir = tempfile.mkdtemp(prefix="alive-codex-judge-")
    items = rep["results"]
    with cf.ThreadPoolExecutor(max_workers=max(1, a.parallel)) as ex:
        futs = {ex.submit(judge_one, codex, it["judge_prompt"], workdir): it for it in items}
        for fut in cf.as_completed(futs):
            it = futs[fut]
            it["judge_codex"] = fut.result()
            j = it["judge_codex"]
            print(f"[{it['id']}#{it.get('rep', 0)}] {'／'.join(it['new'])} -> {'／'.join(it['reply']) or '（无）'}  "
                  f"understood={j.get('understood')} fit={j.get('fits_expectation')} natural={j.get('natural')}"
                  f"{' NARRATES' if j.get('narrates_strategy') else ''} {j.get('issues') or j.get('error') or ''}",
                  flush=True)
    ok = [it["judge_codex"] for it in items if "error" not in it["judge_codex"]]

    def avg(k):
        xs = [float(j.get(k) or 0) for j in ok]
        return round(statistics.mean(xs), 2) if xs else None
    summary = {"judge": "codex", "n": len(items), "judged": len(ok), "understood_mean_0_2": avg("understood"),
               "fits_mean_1_5": avg("fits_expectation"), "natural_mean_1_5": avg("natural"),
               "narrates_rate": round(sum(1 for j in ok if j.get("narrates_strategy")) / max(1, len(ok)), 2)}
    # run-to-run variance (heldout --repeat N): per-repetition means and per-item spread
    reps = sorted({it.get("rep", 0) for it in items})
    if len(reps) > 1:
        def rep_mean(r, k):
            xs = [float(it["judge_codex"].get(k) or 0) for it in items
                  if it.get("rep", 0) == r and "error" not in it["judge_codex"]]
            return round(statistics.mean(xs), 2) if xs else None
        summary["by_rep_fits"] = [rep_mean(r, "fits_expectation") for r in reps]
        summary["by_rep_natural"] = [rep_mean(r, "natural") for r in reps]
        per = {}
        for it in items:
            if "error" not in it["judge_codex"]:
                per.setdefault(it["id"], []).append(int(it["judge_codex"].get("fits_expectation") or 0))
        summary["per_item_fits"] = {k: v for k, v in sorted(per.items())}
        summary["unstable_items"] = sorted(k for k, v in per.items() if len(v) > 1 and max(v) - min(v) >= 2)
    rep["summary_codex"] = summary
    out = a.report.rsplit(".json", 1)[0] + ".codex.json"
    json.dump(rep, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False))
    print("written:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
