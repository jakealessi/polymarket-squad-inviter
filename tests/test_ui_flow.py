import contextlib
import io
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from xml.sax.saxutils import escape

from squad_inviter import Journal, Polymarket, discover, send


def node(kind, label="", x=0, y=0, width=100, height=20, extra=""):
    return (f'<XCUIElementType{kind} label="{escape(label)}" x="{x}" y="{y}" '
            f'width="{width}" height="{height}" {extra}/>')


class SimulatedDevice:
    bundle = "com.polymarket.ios-app"

    def __init__(self):
        self.picker = False
        self.query = ""
        self.chosen = []
        self.invited = {"mattalready"}
        self.submitted = []
        self.opened = 0
        self.rows = {}

    def post(self, path, data):
        assert path == "/execute/sync"
        assert data["script"] == "mobile: activeAppInfo"
        return {"bundleId": self.bundle}

    def get(self, path):
        assert path == "/source"
        xml = f'<XCUIElementTypeApplication x="0" y="0" width="393" height="852" bundleId="{self.bundle}">'
        if not self.picker:
            xml += node("StaticText", "The Matts", 140, 48)
            xml += node("Button", "Invite friends", 40, 120)
        else:
            xml += node("StaticText", "Find Friends", 130, 90)
            xml += node("TextField", "", 20, 140, 350, 36, f'value="{escape(self.query)}"')
            xml += node("StaticText", "Find friends from contacts", 68, 200, 200)
            rows = [name for name in ("matt", "mattalready", "mattp") if name.startswith(self.query)]
            if rows:
                xml += node("StaticText", f"{len(rows)} on Polymarket", 23, 269, 150)
            else:
                xml += node("StaticText", "No results found for this search", 50, 350, 300)
            self.rows = {}
            for index, handle in enumerate(rows):
                y = 315 + index * 48
                xml += node("StaticText", handle, 70, y)
                if handle in self.invited:
                    xml += node("StaticText", "Already invited", 70, y + 17)
                else:
                    self.rows[y + 10] = handle
                    xml += node("Button", "", 342, y, 22, extra='value="1"' if handle in self.chosen else "")
            if self.chosen:
                xml += node("Button", f"Invite {len(self.chosen)}", 25, 775, 343, 47)
        return xml + "</XCUIElementTypeApplication>"

    def change_text(self, field, query):
        assert self.picker and field.attributes["value"] == self.query
        self.query = query

    def control_hittable(self, control):
        return not self.picker and control is not None

    def tap_control(self, control):
        self.tap(*control.center)

    def tap(self, x, y):
        if not self.picker:
            assert y == 130
            self.opened += 1
            self.picker = True
        elif y >= 775:
            assert self.chosen
            self.submitted.append(self.chosen.copy())
            self.invited.update(self.chosen)
            self.chosen.clear()
            self.picker = False
        else:
            assert y in self.rows
            self.chosen.append(self.rows[y])


with tempfile.TemporaryDirectory() as path, contextlib.redirect_stdout(io.StringIO()), patch(
    "squad_inviter.time.sleep"
):
    args = NS(bundle_id=SimulatedDevice.bundle, prefix="matt", squad="The Matts", alphabet="ab", open_via=None,
              search_delay=0, state=Path(path, "run.json"))
    device = SimulatedDevice()
    market = Polymarket(device, args)
    assert market.open_picker().heading
    journal = Journal(args)
    discover(market, journal)
    assert journal.data["candidates"] == {"matt": "matt", "mattp": "mattp"}
    send(market, journal, batch_size=1)
    assert device.submitted == [["matt"], ["mattp"]]
    assert device.opened == 3
    assert journal.data["confirmed"] == ["matt", "mattp"]
    assert journal.data["pending"] == []
print("Passed simulated Appium UI flow from named Squad through search, selection, submission, reopening and verification.")


class ScrollingDevice(SimulatedDevice):
    def __init__(self):
        super().__init__()
        self.picker = True
        self.page = 0
        self.moves = []

    def change_text(self, field, query):
        super().change_text(field, query)
        self.page = 0

    def touch(self, x, start_y, end_y):
        assert 0 < x < 393
        assert 0 < end_y < start_y < 852
        self.moves.append((x, start_y, end_y))
        self.page += 1

    def get(self, path):
        assert path == "/source"
        xml = f'<XCUIElementTypeApplication x="0" y="0" width="393" height="852" bundleId="{self.bundle}">'
        xml += node("StaticText", "Find Friends", 130, 90)
        xml += node("TextField", "", 20, 140, 350, 36, f'value="{escape(self.query)}"')
        if self.page == 0:
            xml += node("StaticText", "20 on Polymarket", 23, 269, 150)
        for index, n in enumerate(range(0, 11) if self.page == 0 else range(9, 20)):
            xml += node("StaticText", f"matt{n}", 70, 300 + index * 48)
        return xml + "</XCUIElementTypeApplication>"


with patch("squad_inviter.time.sleep"):
    device = ScrollingDevice()
    market = Polymarket(device, args)
    count, rows = market.collect("matt")
    assert count == 20
    assert set(rows) == {f"matt{n}" for n in range(20)}
    assert len(device.moves) == 1
print("Passed simulated scroll with overlapping pages and an offscreen result-count header.")


class DirectSelectionDevice(SimulatedDevice):
    def __init__(self):
        super().__init__()
        self.queries = []

    def change_text(self, field, query):
        super().change_text(field, query)
        self.queries.append(query)


from squad_inviter import VisibleBatch
with tempfile.TemporaryDirectory() as path, contextlib.redirect_stdout(io.StringIO()), patch(
    "squad_inviter.time.sleep"
):
    args.state = Path(path, "run.json")
    device = DirectSelectionDevice()
    market = Polymarket(device, args)
    market.open_picker()
    journal = Journal(args)
    batch = VisibleBatch(market, journal, 80)
    discover(market, journal, batch)
    assert device.chosen == ["matt", "mattp"]
    assert device.queries == ["matt"]
    batch.flush()
    assert device.submitted == [["matt", "mattp"]]
    assert device.queries == ["matt", "matt"]
    assert journal.data["confirmed"] == ["matt", "mattp"]
    assert journal.data["pending"] == []
print("Passed direct selection and verification from the same uncapped prefix, with no exact-handle lookups.")


from squad_inviter import Appium, Element, Stop


class ChatAudit:
    def screen(self):
        events = ["old", "matt1", "matt2", "matt3"]
        return NS(heading=False, exact=lambda label: label == "The Matts", invite_activity=lambda _: events)
    args = NS(squad="The Matts")


chat = ChatAudit()
assert Polymarket.verify_chat_activity(chat, "example-owner", ["old"], ["matt1", "matt2", "matt3", "matt4"]) == {
    "matt1", "matt2", "matt3"
}
assert Polymarket.verify_chat_activity(chat, "example-owner", ["old", "old"], ["matt1"]) == set()
assert Polymarket.verify_chat_activity(chat, "example-owner", ["old"], ["matt1", "matt1"]) == set()

appium = Appium.__new__(Appium)
appium.search_element = None
calls = []
ids = iter(("first", "fresh"))

def post(route, data):
    calls.append(route)
    if route == "/elements":
        return [{"element-6066-11e4-a52e-4f735466cecf": next(ids)}]
    if route == "/element/first/clear" and calls.count(route) == 3:
        raise Stop("stale element")
    return None

appium.post = post
field = Element("XCUIElementTypeTextField", {"x":"20", "y":"134", "name":"cui.button.search", "value":"matt"})
for text in ("matta", "mattb", "mattc"):
    appium.change_text(field, text)
assert calls.count("/elements") == 2
assert calls[-1] == "/element/fresh/value"
print("Passed chat-anchor verification, duplicate-handle fallback and cached field recovery.")
