"""Settings and private paths. Everything lives under ANCESTRY_CLI_HOME (default ~/.ancestry-cli); nothing is
hard-coded to a machine.

config.json (all optional). Simple form:
    {"port": 9224, "sandbox_trees": [123], "write_trees": [123, 456], "trees": {"main": 456, "sandbox": 123},
     "account_id": "<guid from `ancestry whoami --pin`>"}
Profile form (several accounts/browsers; pick with --profile or ANCESTRY_CLI_PROFILE):
    {"default_profile": "main", "profiles": {"main": {...as above...}, "other": {...}}}

* sandbox_trees: test trees; live writes there need no --confirm-tree. Every other tree needs it (a typo guard).
* write_trees: if present, the only trees live writes may touch (the real boundary). Absent = no allowlist.
* trees: aliases usable wherever a tree id is expected.
* account_id: pins the signed-in account; a write is refused when the page shows a different one."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

HOME = Path(os.environ.get("ANCESTRY_CLI_HOME", str(Path.home() / ".ancestry-cli")))
_MAX_TREE_ID = 999_999_999_999
_KEYS = ("port", "chrome_profile", "account_id", "sandbox_trees", "write_trees", "trees")


def _raw():
    try:
        data = json.loads((HOME / "config.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def profile_name():
    raw = _raw()
    return os.environ.get("ANCESTRY_CLI_PROFILE") or raw.get("default_profile") or "default"


def unknown_profile():
    """The requested profile's name when it is not defined in config.json (never fall back to another profile's rights)."""
    name, profiles = profile_name(), _raw().get("profiles")
    if name == "default" and not (isinstance(profiles, dict) and "default" in profiles):
        return None
    return None if isinstance(profiles, dict) and name in profiles else name


def chrome_profile():
    """Folder holding the dedicated Chrome's sign-in: ANCESTRY_CLI_CHROME_PROFILE, the profile's chrome_profile, or <home>/chrome-profile."""
    return Path(os.environ.get("ANCESTRY_CLI_CHROME_PROFILE") or profile().get("chrome_profile") or HOME / "chrome-profile").expanduser()


def profile():
    raw, name = _raw(), profile_name()
    profiles = raw.get("profiles")
    if isinstance(profiles, dict) and isinstance(profiles.get(name), dict):
        return profiles[name]
    return {k: raw[k] for k in _KEYS if k in raw}      # the simple form is the "default" profile


def settings():
    return profile()


def port():
    return int(os.environ.get("ANCESTRY_CLI_PORT") or profile().get("port") or 9224)


def sandbox_trees():
    return frozenset(t for t in profile().get("sandbox_trees", []) if isinstance(t, int))


def write_trees():
    """The allowlist, or None when none is configured."""
    wt = profile().get("write_trees")
    return frozenset(t for t in wt if isinstance(t, int)) if isinstance(wt, list) else None


def writable(tree_id):
    wt = write_trees()
    return wt is None or tree_id in wt


def aliases():
    return {k: v for k, v in (profile().get("trees") or {}).items() if isinstance(v, int)}


def resolve_tree(value):
    """'123' / 123 / 'sandbox' -> 123. Raises ValueError for anything else."""
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    if text in aliases():
        return aliases()[text]
    raise ValueError("unknown-tree")


def account_id():
    value = profile().get("account_id")
    return value if isinstance(value, str) and value else None


def pin_account(guid):
    """Write account_id into the active profile of config.json."""
    raw = _raw()
    name = profile_name()
    if isinstance(raw.get("profiles"), dict) and name in raw["profiles"]:
        raw["profiles"][name]["account_id"] = guid
    else:
        raw["account_id"] = guid
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "config.json").write_text(json.dumps(raw, indent=2) + "\n")


def journal_path():
    return Path(os.environ.get("ANCESTRY_CLI_JOURNAL") or HOME / "journal.jsonl")


def snapshot_dir():
    return Path(os.environ.get("ANCESTRY_CLI_SNAPSHOTS") or HOME / "snapshots")


def lock_dir():
    """Fixed per-user location, so two config homes sharing one Chrome still serialize (the lock is per port)."""
    return Path(tempfile.gettempdir()) / f"ancestry-cli-locks-{os.getuid()}"


def lock_file():
    """ANCESTRY_CLI_LOCK_FILE shares one lock with other tools on the same account; default is per Chrome port."""
    override = os.environ.get("ANCESTRY_CLI_LOCK_FILE")
    return Path(override) if override else lock_dir() / f"port-{port()}.lock"


def tree_allowed(tree_id):
    return type(tree_id) is int and 0 < tree_id <= _MAX_TREE_ID


def needs_confirmation(tree_id, confirm_tree):
    return tree_id not in sandbox_trees() and confirm_tree != tree_id
