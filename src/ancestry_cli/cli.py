"""ancestry CLI. Every command prints one JSON object. Exit codes: 0 ok, 1 the operation failed (nothing changed), 2 bad usage,
3 a request was sent and its result is unknown (stop and run `ancestry journal verify`).
Failing results carry `classification`, `retryable`, `needs_human` and a `hint`."""
import argparse
import json
import os
import sys

from . import __version__, config


def _problems_from(message):
    """Turn an argparse message into structured problems: option names and allowed choices, never the user's values."""
    import re

    from .runtime import suggest
    problems = []
    if m := re.match(r"argument (\S+): invalid choice: '(.*?)' \(choose from (.*)\)$", message):
        valid = [c.strip("' ") for c in m.group(3).split(", ")]
        problems.append({"field": m.group(1), "issue": "unknown-value", "valid_values": valid, "did_you_mean": suggest(m.group(2), valid)})
    elif m := re.match(r"argument (\S+): unknown-tree:(.*)$", message):
        names = sorted(config.aliases())
        problems.append({"field": m.group(1), "issue": "unknown-value", "valid_values": names + ["<a numeric tree id>"],
                         "did_you_mean": suggest(m.group(2), names)})
    elif m := re.match(r"argument (\S+): invalid int value", message):
        problems.append({"field": m.group(1), "issue": "invalid", "expected": "an integer"})
    elif m := re.match(r"the following arguments are required: (.*)$", message):
        problems += [{"field": name.strip(), "issue": "missing"} for name in m.group(1).split(",")]
    elif m := re.match(r"unrecognized arguments: (.*)$", message):
        problems += [{"field": tok.split("=", 1)[0], "issue": "not-allowed"} for tok in m.group(1).split() if tok.startswith("-")]
    elif m := re.match(r"argument (\S+): expected one argument", message):
        problems.append({"field": m.group(1), "issue": "missing", "expected": "a value"})
    elif m := re.match(r"argument (\S+): not allowed with argument (\S+)", message):
        problems.append({"field": m.group(1), "issue": "not-allowed", "expected": f"not together with {m.group(2)}"})
    return problems


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse messages echo the user's input; keep only structure (see _problems_from)
        from .runtime import annotate, failure
        problems = _problems_from(message)
        code = "unknown-tree" if "unknown-tree" in message else "usage-error"
        text = f"{len(problems)} problem{'s' if len(problems) != 1 else ''} with the arguments. See `problems`." if problems else None
        result = failure(code, problems=problems, **({"message": text} if text else {}))
        print(json.dumps(annotate(result)))
        raise SystemExit(2)


def _tree(value):
    try:
        return config.resolve_tree(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"unknown-tree:{value}") from None


def _fields(pairs):
    return dict(kv.split("=", 1) for kv in pairs if "=" in kv)


def _modes(p):
    m = p.add_mutually_exclusive_group()
    m.add_argument("--dry-run", dest="dry_run", action="store_true", help="show what would be sent (default)")
    m.add_argument("--live", dest="dry_run", action="store_false", help="send it")
    p.set_defaults(dry_run=True)


def _tree_arg(p, required=False):
    p.add_argument("--tree", dest="tree_id", required=required, type=_tree, help="tree id or an alias from config.json")


def build_parser():
    from .ops import OPS
    parser = _Parser(prog="ancestry", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--profile", help="config profile (accounts/browsers); default from config or ANCESTRY_CLI_PROFILE")
    subs = parser.add_subparsers(dest="command", required=True)

    subs.add_parser("init", help="first-time setup: open Chrome, you sign in, the account is pinned")
    subs.add_parser("doctor", help="check the browser lane, sign-in, account, trees and journal")
    p = subs.add_parser("whoami", help="which Ancestry account is signed in"); p.add_argument("--pin", action="store_true", help="save it as account_id")
    subs.add_parser("trees", help="your owned and shared trees")

    p = subs.add_parser("find", help="find people in a tree by name and years", allow_abbrev=False)
    _tree_arg(p, True)
    p.add_argument("--given"); p.add_argument("--surname")
    p.add_argument("--birth", type=int); p.add_argument("--death", type=int)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--include-living", action="store_true", help="do not redact people who may be living")
    p.add_argument("--complete", action="store_true", help="use the tree's full search (every page): no result means not in the tree")

    p = subs.add_parser("person", help="facts, sources and family of one person", allow_abbrev=False)
    _tree_arg(p, True)
    p.add_argument("--person", dest="person_id", required=True, type=int)
    p.add_argument("--include-living", action="store_true")

    p = subs.add_parser("lane", help="browser tab control"); p.add_argument("action", choices=("reset",))

    p = subs.add_parser("ops", help="describe the write operations: required fields, risk, undo, an example")
    p.add_argument("name", nargs="?", choices=OPS, help="one operation (default: all)")

    p = subs.add_parser("write", help="add, edit or remove tree content", allow_abbrev=False)
    p.add_argument("op", choices=OPS)
    _tree_arg(p, True)
    p.add_argument("--person", dest="person_id", required=True, type=int)
    p.add_argument("--assertion", dest="assertion_id")
    p.add_argument("--set", dest="fields", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--confirm-tree", dest="confirm_tree", type=_tree, help="repeat the tree id to allow a live write outside a sandbox tree")
    p.add_argument("--force", action="store_true", help="repeat a write identical to one recorded in the last 24 hours")
    _modes(p)

    p = subs.add_parser("read", help="search, hints, records and any known read endpoint", allow_abbrev=False)
    p.add_argument("action", choices=("list", "get", "search", "hints", "record", "image"))
    p.add_argument("name", nargs="?", help="endpoint name for `get`")
    _tree_arg(p)
    p.add_argument("--person", dest="person_id", type=int)
    p.add_argument("--path", dest="path_params", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--param", dest="params", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--full", action="store_true", help="return the data, not just its shape")
    for option in ("given", "surname", "birth", "death", "location"):
        p.add_argument("--" + option)
    p.add_argument("--collection", type=int)
    p.add_argument("--record", dest="record_id", type=int)
    p.add_argument("--counts", action="store_true")
    for who in ("spouse", "child"):
        p.add_argument(f"--{who}-given", help=f"{who}'s given name: a couple search ranks records that name them together")
        p.add_argument(f"--{who}-surname")
    p.add_argument("--image", dest="image_id", help="image id, for `read image` (from a search result's imageIds)")
    p.add_argument("--crop", help="x,y,w,h in pixels, or fractions of the image (all <= 1), for `read image`")
    p.add_argument("--out", help="new file to write, for `read image`")

    p = subs.add_parser("collections", help="which record collections have hits (find), what one searches on (describe), what we have seen (list)", allow_abbrev=False)
    p.add_argument("action", choices=("find", "describe", "list"))
    p.add_argument("id", nargs="?", type=int, help="collection id, for describe")
    for option in ("given", "surname", "birth", "death", "location", "keyword", "category", "query"):
        p.add_argument("--" + option)

    p = subs.add_parser("match", help="score search hits against a person's facts (exact / similar / different)", allow_abbrev=False)
    for option in ("given", "surname", "birth", "death", "location"):
        p.add_argument("--" + option)
    p.add_argument("--collection", type=int)
    p.add_argument("--spouse-given"); p.add_argument("--spouse-surname")
    p.add_argument("--limit", type=int, default=5, help="how many hits to score (max 20)")

    p = subs.add_parser("newspapers", help="Newspapers.com page facts: paper, access, names mentioned (needs your signed-in tab)", allow_abbrev=False)
    p.add_argument("action", choices=("page",))
    p.add_argument("--page", dest="page_id", help="Newspapers.com page id (from the Ancestry record's View image link)")

    p = subs.add_parser("census", help="everyone indexed on one record image (a census page), grouped by household", allow_abbrev=False)
    p.add_argument("--collection", type=int, required=True)
    p.add_argument("--image", dest="image_id", help="image id (from a search result's imageIds)")
    p.add_argument("--record", dest="record_id", type=int, help="a record id on the page: finds its image and marks it")
    p.add_argument("--surname", help="keep only households with someone whose name has all these words")
    p.add_argument("--out", help="also write every person and column to this new CSV file")

    p = subs.add_parser("sources", help="list or find the tree's custom sources by title (reuse before creating)", allow_abbrev=False)
    _tree_arg(p)
    p.add_argument("--person", dest="person_id", type=int, help="optional if the account is pinned (`whoami --pin`)")
    p.add_argument("--query", help="words that must all appear in the title")

    p = subs.add_parser("apply", help="run a JSON manifest of writes: validate all, canary first, stop on a problem, write a receipt", allow_abbrev=False)
    p.add_argument("--manifest", required=True)
    p.add_argument("--live", action="store_true", help="send for real (default: validate only)")

    p = subs.add_parser("hint", help="list a person's hints or accept one", allow_abbrev=False)
    p.add_argument("action", choices=("list", "accept", "parents"))
    _tree_arg(p, True)
    p.add_argument("--person", dest="person_id", required=True, type=int)
    p.add_argument("--hint-id")
    p.add_argument("--include-living", action="store_true", help="parents: do not redact a suggestion who may be living")
    p.add_argument("--cite-only", action="store_true")
    p.add_argument("--confirm-tree", dest="confirm_tree", type=_tree)
    p.add_argument("--force", action="store_true")
    _modes(p)

    p = subs.add_parser("journal", help="list what was created, undo it, or settle an unknown outcome", allow_abbrev=False)
    p.add_argument("action", choices=("list", "undo", "verify", "resolve"))
    p.add_argument("--id", dest="entry_id", type=int)
    p.add_argument("--all", dest="all_entries", action="store_true")
    p.add_argument("--as", dest="resolve_as", choices=("ok", "failed"), help="for `resolve`: did the change happen?")
    _modes(p)
    return parser


def doctor():
    from .journal import manual
    from .runtime import (
        LaneError,
        Lease,
        breaker_until,
        get_json,
        guarded,
        load_bridge,
        session,
    )
    out = {"ok": False, "classification": "doctor", "home": str(config.HOME), "profile": config.profile_name(),
           "port": config.port(), "chrome_profile": str(config.chrome_profile()), "sandbox_trees": sorted(config.sandbox_trees()),
           "write_trees": sorted(config.write_trees()) if config.write_trees() is not None else None, "checks": {}}
    checks = out["checks"]
    until = breaker_until()
    if config.write_trees() is None:
        checks["write_trees_warning"] = "no write_trees allowlist, so every tree you own is writable"
    checks["bot_challenge_block"] = "ok" if not until else f"blocked-until-{int(until)}"
    unknown = [r["id"] for r in manual() if r.get("outcome") == "unknown"]
    checks["unknown_outcomes"] = "ok" if not unknown else f"{len(unknown)} unresolved (journal ids {unknown[:5]})"
    try:
        Lease(load_bridge()).check()
        checks["browser_lane"] = "ok"
    except LaneError as exc:
        from .runtime import annotate
        checks["browser_lane"] = exc.code
        out.update(annotate({"ok": False, "classification": exc.code}))
        out["classification"] = "doctor"
        return out

    def probe():
        with session() as (_b, _l, inner):
            data = get_json(inner, "/api/treesui-list/trees", {"rights": "own", "page": "0", "limit": "100"})
        return {"trees": data.get("trees", [])}
    res = guarded(probe)
    if "trees" not in res:
        checks["page_fetch"] = res.get("classification")
        checks["signed_in"] = "not-signed-in" if res.get("classification") == "session-expired" else "unknown"
        from .runtime import annotate
        out["detail"] = annotate(dict(res))
        return out
    checks["page_fetch"] = "ok"
    checks["signed_in"] = "ok"
    owned = {int(t["id"]) for t in res["trees"]}
    owners = {t.get("ownerUserId") for t in res["trees"]}
    pinned = config.account_id()
    checks["account"] = "not-pinned (run `ancestry whoami --pin`)" if not pinned else ("ok" if pinned in owners else "wrong-account")
    for label, trees in (("sandbox_trees", config.sandbox_trees()), ("write_trees", config.write_trees() or ())):
        missing = sorted(t for t in trees if t not in owned)
        checks[label] = "ok" if not missing else f"not in your owned trees: {missing}"
    out["ok"] = all(v == "ok" or str(v).startswith("not-pinned") for v in checks.values())
    return out


def _ops(name=None):
    from .ops import SPECS
    if name:
        return {"ok": True, "classification": "ops", **SPECS[name].describe()}
    return {"ok": True, "classification": "ops",
            "operations": [{"op": n, "risk": sp.risk, "summary": sp.summary, "required": list(sp.required)} for n, sp in SPECS.items()]}


def _run(command, args):
    """Run one parsed command and return its result dict."""
    if command == "init":
        from .setup import init
        return init()
    if command == "census":
        from .census import census
        return census(**args)
    if command == "newspapers":
        from .newspapers import page as newspapers_page
        return newspapers_page(page_id=args["page_id"])
    if command == "match":
        from .match import match
        g, sname = args.pop("spouse_given", None), args.pop("spouse_surname", None)
        args["spouse"] = f"{(g or '').replace(' ', '+')}_{(sname or '').replace(' ', '+')}" if (g or sname) else None
        return match(**args)
    if command == "apply":
        from .apply import apply as apply_manifest
        return apply_manifest(manifest=args["manifest"], live=args["live"])
    if command == "sources":
        from .sources import find as find_sources
        return find_sources(tree_id=args["tree_id"], person_id=args["person_id"], query=args["query"])
    if command == "collections":
        from . import collections as col
        from .runtime import failure
        if args["action"] == "describe":
            return col.describe(args["id"]) if args["id"] else failure("missing-arguments")
        if args["action"] == "list":
            return col.listing(query=args["query"])
        return col.find(**{k: args[k] for k in ("given", "surname", "birth", "death", "location", "keyword", "category")})
    if command == "doctor":
        return doctor()
    if command == "whoami":
        from .discovery import whoami
        return whoami(pin=args["pin"])
    if command == "trees":
        from .discovery import trees
        return trees()
    if command == "find":
        from .discovery import find, find_complete
        return (find_complete if args.pop("complete") else find)(**{k: v for k, v in args.items() if k != "complete"})
    if command == "person":
        from .discovery import person
        return person(**args)
    if command == "ops":
        return _ops(args["name"])
    if command == "lane":
        from .runtime import LaneError, failure, lane_reset
        try:
            return lane_reset()
        except LaneError as exc:
            return failure(exc.code)
    if command == "write":
        from .ops import write
        fields = _fields(args.pop("fields"))
        op = args.pop("op")
        return write(op=op, **args, **{k.replace("-", "_"): v for k, v in fields.items()})
    if command == "read" and args["action"] == "image":
        from .images import save
        return save(collection=args["collection"], image_id=args["image_id"], record_id=args["record_id"], crop=args["crop"], out=args["out"])
    if command == "read":
        from .reads import read
        for extra in ("image_id", "crop", "out"):
            args.pop(extra, None)
        for who in ("spouse", "child"):
            g, sname = args.pop(f"{who}_given", None), args.pop(f"{who}_surname", None)
            args[who] = f"{(g or '').replace(' ', '+')}_{(sname or '').replace(' ', '+')}" if (g or sname) else None
        return read(**{**args, "path_params": _fields(args["path_params"]), "params": _fields(args["params"])})
    if command == "hint":
        from .hints import command as hint_command
        return hint_command(**args)
    from .journal import command as journal_command  # command == "journal"
    return journal_command(**args)


def _apply_profile_flag(argv):
    """--profile must take effect before the other arguments are parsed (tree aliases depend on it)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    for i, a in enumerate(argv):
        if a == "--profile" and i + 1 < len(argv):
            os.environ["ANCESTRY_CLI_PROFILE"] = argv[i + 1]
        elif a.startswith("--profile="):
            os.environ["ANCESTRY_CLI_PROFILE"] = a.split("=", 1)[1]
    return argv


def main(argv=None):
    from .runtime import annotate
    argv = _apply_profile_flag(argv)
    bad = config.unknown_profile()
    if bad:
        from .runtime import failure
        result = annotate(failure("configuration-error", problems=[{"field": "profile", "issue": "unknown-value"}],
                                  hint=f"Profile {bad!r} is not defined in config.json."))
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return 2
    args = vars(build_parser().parse_args(argv))
    command = args.pop("command")
    args.pop("profile", None)
    result = annotate(_run(command, args))
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    if result.get("ok") is True:
        return 0
    return 3 if result.get("state") == "unknown" else 1      # 3 = a request left and its result is unknown: stop and verify


if __name__ == "__main__":
    raise SystemExit(main())
