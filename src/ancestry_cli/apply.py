"""`ancestry apply --manifest FILE`: a batch of writes with a canary, a stop on the first problem, and a receipt.

Manifest (JSON):
  {"tree": 123456789 | "alias", "confirm_tree": 123456789 (only needed outside sandbox trees),
   "operations": [{"op": "fact-add", "person": 111, "assertion": 222 (when the op needs one), "set": {"eventType": "Birth"}}]}
Without --live every step is only validated (no network, no values echoed). With --live the first step runs alone as the canary;
if it did not change what it should, the batch stops. Later steps run in order and stop at the first failure or unknown outcome.
Each result is appended to manifest["receipt"]; steps already recorded as ok there are skipped, so a stopped batch can be re-run.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import config
from .runtime import failure


def _save(path, manifest):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n")
    os.replace(tmp, path)


def _step_fields(step):
    fields = {k.replace("-", "_"): v for k, v in (step.get("set") or {}).items()}
    if step.get("assertion") is not None:
        fields["assertion_id"] = step["assertion"]
    return fields


def apply(*, manifest, live=False):
    from .ops import write
    path = Path(manifest)
    try:
        data = json.loads(path.read_text())
        tree = config.resolve_tree(str(data["tree"]))
        steps = data["operations"]
        assert isinstance(steps, list) and steps and all(isinstance(s, dict) and s.get("op") and s.get("person") for s in steps)
    except (OSError, ValueError, KeyError, AssertionError):
        return failure("invalid-manifest")
    confirm = data.get("confirm_tree")
    # pass 1: nothing is sent until every step validates
    bad = []
    for i, s in enumerate(steps):
        r = write(op=s["op"], tree_id=tree, person_id=int(s["person"]), dry_run=True, **_step_fields(s))
        if not r.get("ok"):
            bad.append({"index": i, "op": s["op"], "classification": r.get("classification"), "problems": r.get("problems")})
    if bad:
        return {**failure("invalid-manifest", problems=bad), "validated": False}
    if not live:
        return {"ok": True, "classification": "apply-dry-run", "dispatch_attempted": False, "state": "unchanged",
                "steps": len(steps), "next": "re-run with --live to send; the first step is the canary"}
    receipt = data.setdefault("receipt", [])
    done = {r["index"] for r in receipt if r.get("ok")}
    sent = 0
    for i, s in enumerate(steps):
        if i in done:
            continue
        r = write(op=s["op"], tree_id=tree, person_id=int(s["person"]), dry_run=False, confirm_tree=confirm, **_step_fields(s))
        row = {"index": i, "op": s["op"], "person": int(s["person"]), "ok": bool(r.get("ok")), "state": r.get("state"),
               "classification": r.get("classification"), "journal_id": r.get("journal_id"), "ids": r.get("ids") or None}
        if isinstance(r.get("readback"), dict):
            row["readback"] = r["readback"]
        receipt.append(row)
        _save(path, data)
        sent += 1
        if not r.get("ok"):
            return {**failure(r.get("classification") or "send-failed", dispatched=True, state=r.get("state") or "unknown"),
                    "stopped_at": i, "canary": i == 0 and not done, "applied": len([x for x in receipt if x.get("ok")]),
                    "journal_id": r.get("journal_id")}
        if i == 0 and not done and r.get("state") != "changed":
            return {**failure("canary-no-change", dispatched=True, state=r.get("state") or "unchanged"), "stopped_at": 0}
    return {"ok": True, "classification": "applied", "dispatch_attempted": sent > 0, "state": "changed" if sent else "unchanged",
            "applied": len([x for x in receipt if x.get("ok")]), "steps": len(steps), "receipt": f"{path.name} (receipt key)"}
