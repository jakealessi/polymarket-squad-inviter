from types import SimpleNamespace as NS
from unittest.mock import patch

from squad_inviter import ACCOUNT_ROW, Polymarket, Row, VisibleBatch


def row(handle):
    return Row(handle, NS(enabled=True), None, ACCOUNT_ROW + handle)


upper = [row(f"johnfirst{i}") for i in range(10)]
lower = [row(f"johnsmith0{i}") for i in range(10)]
anchor = row("johnanchor")
unexpected = row("johnsmith0extra")
parent_ids = {item.key for item in upper + lower}


class Page:
    heading = True
    keyboard = None
    app = NS(x=0, width=402)
    list_view = NS(y=238)

    def __init__(self, device):
        self.query = device.query
        self.selected_accounts = set(device.selected)
        self.selected = len(self.selected_accounts)
        self.count_label = None if device.bottom else NS(center=(100, 270))
        if self.query == "john":
            self.count = 20
            self.visible = lower if device.bottom else upper
        else:
            self.count = 10
            self.visible = lower[:-1] + [unexpected] if device.mismatch else lower

    def rows(self):
        return {item.key: item for item in self.visible}

    def field(self):
        return NS(attributes={"value": self.query})

    def bounds(self):
        return 286, 770

    def selection_point(self, item):
        return 359, 300 + 50 * self.visible.index(item)


class Device:
    def __init__(self, mismatch):
        self.query = "john"
        self.bottom = False
        self.mismatch = mismatch
        self.selected = {anchor.key}
        self.drags = 0

    def touch(self, *args, **kwargs):
        assert self.query == "john"
        self.bottom = True
        self.drags += 1


class Market(Polymarket):
    def __init__(self, device):
        super().__init__(device, NS(scroll_fraction=1.0))
        self.searches = []
        self.groups = []
        self.individual = []

    def screen(self):
        return Page(self.driver)

    def search(self, query):
        self.searches.append(query)
        self.driver.query = query
        self.driver.bottom = False
        return self.screen()

    def select(self, screen, item, expected):
        assert screen.selected == expected and item.key in screen.rows()
        self.individual.append(item.key)
        self.driver.selected.add(item.key)
        self.driver.bottom = False
        return self.screen()

    def select_group(self, screen, items, expected):
        assert screen.selected == expected and not self.driver.bottom
        assert len(items) <= 10 and expected + len(items) <= 80
        assert {item.key for item in items} <= screen.rows().keys()
        self.groups.append(len(items))
        self.driver.selected.update(item.key for item in items)
        return self.screen()


for mismatch in (False, True):
    device = Device(mismatch)
    market = Market(device)
    journal = NS(data={"confirmed": [], "candidates": {}, "origins": {}}, save=lambda: None)
    batch = VisibleBatch(market, journal, 80)
    batch.entries[anchor.key] = {"account": anchor.key, "handle": anchor.handle}
    with patch("squad_inviter.time.sleep"):
        count, found = batch.collect("john")
    assert count == 20 and list(found) == [item.key for item in upper + lower]
    assert set(batch.entries) == device.selected == parent_ids | {anchor.key}
    assert unexpected.key not in journal.data["candidates"]
    if mismatch:
        assert market.searches == ["john", "johnsmith0", "john"]
        assert market.groups == [10] and len(market.individual) == 10
    else:
        assert market.searches == ["john", "johnsmith0"]
        assert market.groups == [10, 10] and not market.individual and device.drags == 1
print("Passed stable ten-row groups, complete parent coverage, and safe fallback on a missing ID.")


class KeyboardMarket(Market):
    def __init__(self, device):
        super().__init__(device)
        self.dismissals = []

    def search(self, query):
        page = super().search(query)
        page.keyboard = NS()
        return page

    def dismiss_keyboard(self, screen, query):
        assert screen.keyboard and screen.query == query
        self.dismissals.append(query)
        return self.screen()


for count, room in ((2, 5), (3, 2), (3, 3)):
    device = Device(False)
    device.bottom = True
    device.selected.update(item.key for item in upper + lower[count:])
    market = KeyboardMarket(device)
    journal = NS(data={"confirmed": [], "candidates": {}, "origins": {}}, save=lambda: None)
    batch = VisibleBatch(market, journal, len(device.selected) + room)
    batch.entries.update((key, {}) for key in device.selected)
    should_group = count >= 3 and room >= 3
    assert batch.try_narrow_group(market.screen(), "john") == should_group
    if not should_group:
        assert not market.searches and not market.groups
    else:
        assert market.searches == market.dismissals == ["johnsmith0"]
        assert market.groups == [3]
        assert set(batch.entries) == parent_ids | {anchor.key}
print("Passed the three-account minimum and keyboard dismissal before a narrowed group.")
