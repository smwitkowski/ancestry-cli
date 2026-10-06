import json
import os
import tempfile
from pathlib import Path

# Every test runs against a private temporary home with one sandbox tree; nothing touches a real journal.
_HOME = Path(tempfile.mkdtemp(prefix="ancestry-cli-test-"))
(_HOME / "config.json").write_text(json.dumps({"port": 9224, "sandbox_trees": [111111111]}))
os.environ["ANCESTRY_CLI_HOME"] = str(_HOME)
