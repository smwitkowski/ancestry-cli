"""`ancestry init`: start the dedicated Chrome, wait for the owner to sign in by hand, pin the account."""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import config

_MAC = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LOGIN = "https://www.ancestry.com/account/signin"


def _chrome():
    if sys.platform == "darwin" and Path(_MAC).exists():
        return _MAC
    return next((p for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
                 if (p := shutil.which(n))), None)


def _listening(port):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2).read()
        return True
    except OSError:
        return False


def init(*, wait=300):
    from .cli import doctor
    from .discovery import whoami
    port = config.port()
    if not _listening(port):
        exe = _chrome()
        if not exe:
            return {"ok": False, "classification": "chrome-not-found", "state": "unchanged"}
        profile = config.HOME / "chrome-profile"
        profile.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                          "--no-first-run", LOGIN], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        for _ in range(20):
            if _listening(port):
                break
            time.sleep(1)
        else:
            return {"ok": False, "classification": "chrome-not-listening", "state": "unchanged"}
    print(json.dumps({"message": "Sign in to Ancestry in the Chrome window that opened. Waiting."}), file=sys.stderr)
    deadline = time.time() + wait
    while time.time() < deadline:
        if doctor()["checks"].get("signed_in") == "ok":
            who = whoami(pin=True)
            return {"ok": True, "classification": "init", "state": "unchanged", "port": port,
                    "account_pinned": bool(who.get("ok")), "next": "run `ancestry doctor`, then `ancestry trees`"}
        time.sleep(5)
    return {"ok": False, "classification": "sign-in-timeout", "state": "unchanged",
            "next": "finish signing in in the Chrome window, then run `ancestry init` again"}
