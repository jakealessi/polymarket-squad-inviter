import contextlib
import io
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from squad_inviter import Journal, Row, Screen, Stop, discover, send, verify_batch

XML = """<AppiumAUT><XCUIElementTypeApplication x="0" y="0" width="393" height="852">
<XCUIElementTypeStaticText label="Find Friends" x="130" y="90" width="110" height="20" />
<XCUIElementTypeTextField value="matt" x="20" y="140" width="350" height="36" />
<XCUIElementTypeStaticText label="Find friends from contacts" x="68" y="200" width="200" height="20" />
<XCUIElementTypeStaticText label="20 on Polymarket" x="23" y="269" width="150" height="18" />
<XCUIElementTypeStaticText label="Matt" x="72" y="306" width="45" height="16" />
<XCUIElementTypeStaticText label="Already invited" x="72" y="326" width="95" height="16" />
<XCUIElementTypeStaticText label="MattA" x="72" y="358" width="60" height="16" />
<XCUIElementTypeButton x="342" y="356" width="21" height="20" />
<XCUIElementTypeStaticText label="matt2, Already a member" x="72" y="410" width="130" height="22" />
<XCUIElementTypeButton label="Invite 3" x="25" y="775" width="343" height="47" />
</XCUIElementTypeApplication></AppiumAUT>"""

screen = Screen(XML, "com.polymarket.ios-app")
assert screen.count == 20
assert screen.selected == 3
assert screen.field().attributes["value"] == "matt"
assert not screen.rows()["matt"].selectable
assert screen.rows()["matt2"].status == "Already a member"
assert screen.selection_control(screen.rows()["matta"]).center == (352.5, 366)
empty = XML.replace('<XCUIElementTypeStaticText label="20 on Polymarket"', '<XCUIElementTypeStaticText label="No results found for this search"')
assert Screen(empty, "com.polymarket.ios-app").count == 0


class FakeMarket:
    def __init__(self):
        self.queries = []
        self.sent = []
        self.selected = []
        self.invited = {"matt-exists"}

    def collect(self, query):
        self.queries.append(query)
        entries = {
            "matt": (20, ["matt", "mattab", "matt-exists"]),
            "matta": (20, ["mattab", "mattaa"]),
            "mattaa": (1, ["mattaa"]),
            "mattab": (1, ["mattab"]),
            "mattb": (1, ["mattb"]),
        }
        count, handles = entries.get(query, (0, []))
        return count, {handle: self.row(handle) for handle in handles}

    def row(self, handle):
        return Row(handle, NS(enabled=True), "Already invited" if handle in self.invited else None)

    def open_picker(self):
        return NS(selected=len(self.selected))

    def exact_row(self, handle, account_id=None):
        return self.open_picker(), self.row(handle)

    def wait_for_invited(self, handle, account_id=None):
        return handle in self.invited


    def select(self, screen, row, expected):
        assert expected == len(self.selected)
        self.selected.append(row.handle)

    def submit(self, handles):
        assert handles == self.selected
        assert not self.invited.intersection(handles)
        self.sent.append(len(handles))
        self.invited.update(handles)
        self.selected = []


with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
    args = NS(state=Path(directory, "run.json"), prefix="matt", squad="The Matts", alphabet="ab")
    fake = FakeMarket()
    journal = Journal(args)
    discover(fake, journal)
    assert fake.queries == ["matt", "matta", "mattaa", "mattab", "mattb"]
    assert set(journal.data["candidates"]) == {"matt", "mattab", "mattaa", "mattb"}
    assert Journal(args).data["remaining"] == []
    journal.data["candidates"] = {f"matt{i}": f"matt{i}" for i in range(165)}
    journal.data["candidates"]["matt-exists"] = "matt-exists"
    send(fake, journal, 80)
    assert fake.sent == [80, 80, 5]
    assert len(Journal(args).data["confirmed"]) == 166
    send(fake, Journal(args), 80)
    assert fake.sent == [80, 80, 5]
    journal.data["pending"] = ["matt-unverified"]
    journal.save()
    try:
        verify_batch(fake, journal)
    except Stop:
        assert Journal(args).data["pending"] == ["matt-unverified"]
    else:
        raise AssertionError("Unverified invites must stay pending")
print("Passed: UI labels, empty results, depth-first cap split, deduplication, 80/80/5 batch limit, resume, and unverified submission.")

from squad_inviter import DEFAULT_ALPHABET, continuation_suffixes
handles = [f"mattb{i:02}" for i in range(19)] + ["mattw"]
ordered_rows = {name: Row(name, NS(enabled=True), None) for name in handles}
assert continuation_suffixes("matt", 20, ordered_rows, DEFAULT_ALPHABET, True) == "._wxyz-"
assert continuation_suffixes("matt", 20, dict(reversed(list(ordered_rows.items()))), DEFAULT_ALPHABET, True) == DEFAULT_ALPHABET
assert continuation_suffixes("matt", 20, dict(list(ordered_rows.items())[:-1]), DEFAULT_ALPHABET, True) == DEFAULT_ALPHABET
assert continuation_suffixes("matt", 20, ordered_rows, DEFAULT_ALPHABET, False) == DEFAULT_ALPHABET
print("Passed cutoff retention, punctuation coverage, and unordered or partial result fallback.")
