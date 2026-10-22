"""Validate registry/*.json against the platform's own route-registry schemas.

The schemas are `CreateEvaluationRouteRequestSchema` and
`CreateRouteVersionRequestSchema` in upstream
`packages/shared-types/src/evaluation-route.ts` (Zod). This script copies that
file from the upstream checkout into a temporary directory, installs the zod
version pinned in upstream's `pnpm-lock.yaml` there with npm, and parses both
JSON files with Node. Nothing is sent anywhere and nothing is registered.

Exit 0: both valid. Exit 1: invalid (errors printed). Exit 77: NOT EXECUTED
(no upstream checkout, Node 22+ or npm, or the npm install failed).

Usage: python registry/validate_declarations.py [--upstream ../upstream]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
NOT_EXECUTED = 77

CHECK = r"""
import { readFileSync } from 'node:fs';
import { CreateEvaluationRouteRequestSchema, CreateRouteVersionRequestSchema } from './evaluation-route.ts';
const out = {};
for (const [name, schema, file] of [
  ['evaluation-route', CreateEvaluationRouteRequestSchema, process.argv[2]],
  ['route-version', CreateRouteVersionRequestSchema, process.argv[3]],
]) {
  const r = schema.safeParse(JSON.parse(readFileSync(file, 'utf8')));
  out[name] = r.success ? 'valid' : r.error.issues;
}
process.stdout.write(JSON.stringify(out));
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", type=Path, default=ROOT.parent / "upstream")
    ap.add_argument("--route", type=Path, default=HERE / "evaluation-route.json")
    ap.add_argument("--version", type=Path, default=HERE / "route-version.json")
    a = ap.parse_args()
    schema = a.upstream / "packages" / "shared-types" / "src" / "evaluation-route.ts"
    lock = a.upstream / "pnpm-lock.yaml"
    node, npm = shutil.which("node"), shutil.which("npm")
    if not schema.exists() or not lock.exists() or node is None or npm is None:
        print("NOT EXECUTED: needs the upstream checkout, Node 22+ and npm")
        return NOT_EXECUTED
    m = re.search(r"^  zod@(\d+\.\d+\.\d+):", lock.read_text(), flags=re.M)
    zod = m.group(1) if m else "4"
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        shutil.copy(schema, work / "evaluation-route.ts")
        (work / "package.json").write_text(json.dumps({"type": "module", "private": True}))
        (work / "check.mjs").write_text(CHECK)
        r = subprocess.run([npm, "install", "--silent", "--no-audit", "--no-fund", f"zod@{zod}"], cwd=work,
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"NOT EXECUTED: npm install zod@{zod} failed: {r.stderr.strip()[-300:]}")
            return NOT_EXECUTED
        r = subprocess.run([node, "--experimental-strip-types", "--no-warnings", "check.mjs",
                            str(a.route.resolve()), str(a.version.resolve())],
                           cwd=work, capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stderr.strip()[-1000:])
            return 1
        res = json.loads(r.stdout)
    print(json.dumps({"zod": zod, **res}, indent=1))
    return 0 if all(v == "valid" for v in res.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
