import json
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import run_squad_queue as queue
from squad_inviter import DEFAULT_ALPHABET, Stop


fixture = Path(__file__).with_name("fixtures") / "squad-directory.xml"
page = queue.DirectoryPage(fixture.read_text())
assert page.header_y is not None
assert [title for title, _ in page.rows] == ["The Apples", "The Oranges", "The Limes"]

taps = []
market = NS(driver=NS(control_hittable=lambda _: True, tap_control=taps.append),
            wait=lambda predicate, description: description)
with patch.object(queue, "pages", lambda *_: iter([page])):
    assert "The Oranges" in queue.directory_target(market, "the oranges")
    assert taps == [page.rows[1][1]]
    try:
        queue.directory_target(market, "The Oranges Test")
    except queue.MissingSquad:
        assert len(taps) == 1
    else:
        raise AssertionError("A partial title was accepted")

page.rows.append(page.rows[1])
with patch.object(queue, "pages", lambda *_: iter([page])):
    try:
        queue.directory_target(market, "The Oranges")
    except queue.AmbiguousSquad:
        assert len(taps) == 1
    else:
        raise AssertionError("An ambiguous title was accepted")

with tempfile.TemporaryDirectory() as folder:
    path = Path(folder).resolve() / "queue.local.json"
    data = {
        "udid": "example-device",
        "jobs": [
            {"id": "short", "prefix": "Ab", "squad": "Example Squad"},
            {"id": "missing", "prefix": "cd", "squad": "Other Squad"},
            {"id": "long", "prefix": "abc", "squad": "Example Squad"},
        ],
    }
    path.write_text(json.dumps(data))
    config = queue.load_config(path)
    config.state_dir.mkdir()
    assert config.state_dir == path.parent / ".squad-inviter"
    assert config.targets["short"].prefix == "ab"
    attempts = []

    def pretend_worker(command, stdout, **_):
        key = command[command.index("--worker") + 1]
        assert Path(command[3]) == path
        target = config.targets[key]
        attempts.append(key)
        if key == "missing":
            stdout.write(b"No matching squad in the complete directory scan\n")
            return NS(returncode=queue.MISSING)
        if key == "short" and attempts.count(key) == 1:
            stdout.write(b"Temporary connection failure\n")
            return NS(returncode=1)
        target.state.write_text(json.dumps({
            "version": 1,
            "identity": {"squad": target.squad, "prefix": target.prefix, "alphabet": DEFAULT_ALPHABET},
            "confirmed": ["example-account"], "searched": 1, "remaining": [],
            "pending": [], "unresolved": {}, "candidates": {"example-account": target.prefix},
        }))
        stdout.write(b"Finished\n")
        return NS(returncode=0)

    with patch.object(queue.subprocess, "run", pretend_worker), patch.object(queue.time, "sleep"):
        assert queue.run_queue(config) == 1
        assert attempts == ["short", "short", "missing", "long"]
        assert queue.run_queue(config) == 1
        assert attempts == ["short", "short", "missing", "long", "missing"]
    report = json.loads((config.state_dir / "squad-queue-status.json").read_text())
    assert report["entries"]["missing"]["status"] == "missing"
    assert report["entries"]["long"]["status"] == "complete"

    data["jobs"][0]["squad"] = "Different Squad"
    path.write_text(json.dumps(data))
    config = queue.load_config(path)
    try:
        queue.read_state(config.targets["short"])
    except Stop:
        pass
    else:
        raise AssertionError("A checkpoint was reused for a different squad")
print("Passed directory matching, job configuration, ordered retries, and checkpoint validation.")
