#!/usr/bin/env python3
"""Polymarket Squad invitations through Appium's iPhone accessibility interface."""

import argparse
import contextlib
import fcntl
import json
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from os.path import commonprefix
from pathlib import Path

RESULT_CAP = 20
BATCH_CAP = 80
DEFAULT_ALPHABET = "0123456789._abcdefghijklmnopqrstuvwxyz-"
USERNAME = re.compile(r"@?([A-Za-z0-9][A-Za-z0-9_.-]{0,63})\Z")
INVITED_STATUS = re.compile(r"already invited|already a member", re.I)
STATUS = re.compile(r"already invited|already a member|can['’]t invite yet", re.I)
COUNT = re.compile(r"(\d+) on Polymarket", re.I)
INVITE_COUNT = re.compile(r"Invite (\d+)", re.I)
ACCOUNT_ROW = "cui.listRow.contact-"
SELECTED_CHIP = "cui.iconButton.remove-"
PICKER_SNAPSHOT_DEPTH = 22


class Stop(RuntimeError):
    pass


class ControlUnavailable(Stop):
    pass


class Appium:
    def __init__(self, args):
        self.base = args.server.rstrip("/")
        capabilities = {
            "platformName": "iOS",
            "appium:automationName": "XCUITest",
            "appium:udid": args.udid,
            "appium:bundleId": args.bundle_id,
            "appium:noReset": True,
            "appium:autoLaunch": False,
            "appium:forceAppLaunch": False,
            "appium:shouldTerminateApp": False,
            "appium:newCommandTimeout": 180,
            "appium:wdaBindingIP": "127.0.0.1",
        }
        if args.wda_url:
            capabilities["appium:webDriverAgentUrl"] = args.wda_url
        if getattr(args, "wda_derived_data", None):
            capabilities["appium:derivedDataPath"] = str(args.wda_derived_data.resolve())
        if args.team_id:
            capabilities.update({
                "appium:xcodeOrgId": args.team_id,
                "appium:xcodeSigningId": "Apple Development",
                "appium:updatedWDABundleId": args.wda_bundle_id,
                "appium:allowProvisioningDeviceRegistration": True,
            })
        value = self.request("POST", "/session", {"capabilities": {"alwaysMatch": capabilities}})
        self.session = "/session/" + value["sessionId"]
        self.search_element = None
        self.picker_snapshot = False
        self._full_snapshot_depth = None
        try:
            self.post("/appium/settings", {"settings": {
                "pageSourceExcludedAttributes": "accessible,focused,visible,type,index,traits",
            }})
        except Stop:
            with contextlib.suppress(Stop):
                self.close()
            raise

    def request(self, method, route, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + route, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as response:
                return json.load(response)["value"]
        except urllib.error.HTTPError as error:
            raw = error.read().decode(errors="replace")
            try:
                message = json.loads(raw)["value"]["message"]
            except (ValueError, KeyError, TypeError):
                message = raw
            raise Stop(f"Appium rejected {route.split('/')[-1]}: {message.splitlines()[0]}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise Stop(f"Appium connection failed at {self.base}: {error}") from error

    def get(self, route):
        return self.request("GET", self.session + route)

    def post(self, route, body):
        # Invalidate any reusable input context before a potentially changing
        # command. Changing only the animation timeout leaves the view intact.
        if route != "/appium/settings" or body.get("settings", {}).keys() != {"animationCoolOffTimeout"}:
            self.ui_revision = getattr(self, "ui_revision", 0) + 1
        return self.request("POST", self.session + route, body)

    def close(self):
        if getattr(self, "picker_snapshot", False):
            with contextlib.suppress(Stop):
                self.set_picker_snapshot(False)
        self.request("DELETE", self.session)

    def set_picker_snapshot(self, enabled):
        if enabled == getattr(self, "picker_snapshot", False):
            return
        if getattr(self, "_full_snapshot_depth", None) is None:
            self._full_snapshot_depth = self.get("/appium/settings").get("snapshotMaxDepth", 0)
        if not self._full_snapshot_depth:
            return
        depth = PICKER_SNAPSHOT_DEPTH if enabled else self._full_snapshot_depth
        self.post("/appium/settings", {"settings": {"snapshotMaxDepth": depth}})
        self.picker_snapshot = enabled

    def tap(self, x, y):
        self.touch(x, y)

    @contextlib.contextmanager
    def selection_timing(self):
        cooldown = getattr(self, "_selection_cooldown", None)
        if cooldown is None:
            cooldown = self.get("/appium/settings").get("animationCoolOffTimeout", 0)
            self._selection_cooldown = cooldown
        try:
            if cooldown:
                self.post("/appium/settings", {"settings": {"animationCoolOffTimeout": 0}})
            yield bool(cooldown)
        finally:
            if cooldown:
                self.post("/appium/settings", {"settings": {"animationCoolOffTimeout": cooldown}})

    def tap_sequence(self, points):
        settings = self.get("/appium/settings")
        cooldown = settings.get("animationCoolOffTimeout", 2)
        if cooldown:
            self.post("/appium/settings", {"settings": {"animationCoolOffTimeout": 0}})
        try:
            for point in points:
                self.tap(*point)
        finally:
            if cooldown:
                self.post("/appium/settings", {"settings": {"animationCoolOffTimeout": cooldown}})

    def tap_event(self, points, *, spacing_ms=425):
        """Send one iOS event with an independent touch path for each point."""
        sources = []
        for index, (x, y) in enumerate(points):
            actions = []
            if index:
                actions.append({"type": "pause", "duration": index * spacing_ms})
            actions.extend([
                {"type": "pointerMove", "duration": 0, "origin": "viewport",
                 "x": round(x), "y": round(y)},
                {"type": "pointerDown", "button": 0},
                {"type": "pause", "duration": 25},
                {"type": "pointerUp", "button": 0},
            ])
            sources.append({"type": "pointer", "id": f"invite-touch-{index}",
                            "parameters": {"pointerType": "touch"}, "actions": actions})
        self.post("/actions", {"actions": sources})

    def control_element(self, control):
        attributes = control.attributes
        position = f"[@x='{attributes['x']}' and @y='{attributes['y']}']"
        name = attributes.get("name", "")
        if name and '"' not in name:
            position += f'[@name="{name}"]'
        matches = self.post("/elements", {"using": "xpath", "value": f"//{control.kind}{position}"})
        if len(matches) != 1:
            return None
        element = matches[0]["element-6066-11e4-a52e-4f735466cecf"]
        if str(self.get(f"/element/{element}/attribute/hittable")).lower() != "true":
            return None
        return element

    def control_hittable(self, control):
        with contextlib.suppress(Stop):
            return self.control_element(control) is not None
        return False

    def tap_control(self, control):
        element = self.control_element(control)
        if element is None:
            raise ControlUnavailable("The invite control is no longer uniquely available or is covered.")
        self.post(f"/element/{element}/click", {})

    def touch(self, x, y, end_y=None, *, drag_ms=430, hold_ms=0):
        actions = [
            {"type": "pointerMove", "duration": 0, "origin": "viewport", "x": round(x), "y": round(y)},
            {"type": "pointerDown", "button": 0},
        ]
        if end_y is not None:
            actions.append({
                "type": "pointerMove", "duration": drag_ms, "origin": "viewport",
                "x": round(x), "y": round(end_y),
            })
            if hold_ms:
                actions.append({"type": "pause", "duration": hold_ms})
        actions.append({"type": "pointerUp", "button": 0})
        self.post("/actions", {"actions": [{
            "type": "pointer", "id": "squad-inviter", "parameters": {"pointerType": "touch"},
            "actions": actions,
        }]})

    def change_text(self, field, text, incremental=False):
        key = field.kind, field.attributes.get("name"), field.attributes["x"], field.attributes["y"]
        value = field.attributes.get("value", "")
        overlap = len(commonprefix((value, text))) if incremental else 0
        edit = "\b" * (len(value) - overlap) + text[overlap:] if overlap and value != text else None
        for attempt in range(2):
            cached = self.search_element is not None and self.search_element[0] == key
            if not cached:
                self.search_element = key, self.find_search_element(field)
            element = self.search_element[1]
            try:
                if edit and not attempt:
                    self.post(f"/element/{element}/value", {"text": edit, "value": list(edit)})
                    return True
                if attempt or value and value != field.attributes.get("placeholderValue"):
                    self.post(f"/element/{element}/clear", {})
                self.post(f"/element/{element}/value", {"text": text, "value": list(text)})
                return False
            except Stop:
                self.search_element = None
                if not cached or attempt:
                    raise

    def find_search_element(self, field):
        attributes = field.attributes
        xpath = f"//{field.kind}[@x='{attributes['x']}' and @y='{attributes['y']}']"
        identifier = attributes.get("name", "")
        for attempt in range(2):
            found = (self.post("/elements", {"using": "accessibility id", "value": identifier})
                     if identifier.startswith("cui.") else [])
            if not found:
                found = self.post("/elements", {"using": "xpath", "value": xpath})
            ids = list(dict.fromkeys(item["element-6066-11e4-a52e-4f735466cecf"] for item in found))
            if len(ids) > 1:
                ids = [element for element in ids
                       if str(self.get(f"/element/{element}/attribute/hittable")).lower() == "true"]
            if len(ids) == 1:
                return ids[0]
            if not attempt:
                time.sleep(0.35)
        raise Stop("Could not uniquely identify the Find Friends search field.")


@dataclass(frozen=True)
class Element:
    kind: str
    attributes: dict

    def number(self, name):
        try:
            return float(self.attributes.get(name, 0))
        except ValueError:
            return 0

    @property
    def x(self):
        return self.number("x")

    @property
    def y(self):
        return self.number("y")

    @property
    def width(self):
        return self.number("width")

    @property
    def height(self):
        return self.number("height")

    @property
    def center(self):
        return self.x + self.width / 2, self.y + self.height / 2

    def contains(self, point):
        x, y = point
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height

    @property
    def visible(self):
        return self.attributes.get("visible") != "false" and self.width > 0 and self.height > 0

    @property
    def enabled(self):
        return self.attributes.get("enabled") != "false"

    @property
    def texts(self):
        return tuple(dict.fromkeys(
            self.attributes[key].strip()
            for key in ("label", "name", "value")
            if self.attributes.get(key, "").strip()
        ))


@dataclass(frozen=True)
class Row:
    handle: str
    node: Element
    status: str | None
    account_id: str | None = None

    @property
    def key(self):
        return self.account_id or self.handle.casefold()

    @property
    def selectable(self):
        return self.node.enabled and self.status is None

    @property
    def invited(self):
        return bool(self.status and INVITED_STATUS.fullmatch(self.status))


def unique_location(elements, name):
    by_location = {element.center: element for element in elements}
    if len(by_location) > 1:
        raise Stop(f"Found multiple visible controls for {name!r}.")
    return next(iter(by_location.values()), None)


class Screen:
    def __init__(self, xml, bundle_id):
        root = ET.fromstring(xml)
        elements = {node: Element(node.tag, node.attrib) for node in root.iter()}
        nodes = list(elements.values())
        self.row_accounts = {
            id(elements[child]): cell.attrib["name"]
            for cell in root.iter("XCUIElementTypeCell")
            if cell.attrib.get("name", "").startswith(ACCOUNT_ROW)
            for child in cell.iter()
        }
        applications = [node for node in nodes if node.kind == "XCUIElementTypeApplication"]
        if not applications or applications[0].width <= 0 or applications[0].height <= 0:
            raise Stop("Appium did not return an iPhone application window.")
        self.app = applications[0]
        if self.app.attributes.get("bundleId", bundle_id) != bundle_id:
            raise Stop("Polymarket is no longer in the foreground.")
        self.nodes = [node for node in nodes if node.visible and node is not self.app]
        self.chat_events = self.find_chat_activity(elements)
        self.selected_accounts = {
            ACCOUNT_ROW + name.removeprefix(SELECTED_CHIP)
            for node in self.nodes
            if node.kind == "XCUIElementTypeButton"
            and (name := node.attributes.get("name", "")).startswith(SELECTED_CHIP)
        }
        self.alert = next((node for node in self.nodes if node.kind == "XCUIElementTypeAlert"), None)
        # iOS can keep the dismissed keyboard in the accessibility tree below the window.
        self.keyboard = next((node for node in self.nodes if node.kind == "XCUIElementTypeKeyboard"
                              and node.x < self.app.x + self.app.width and node.x + node.width > self.app.x
                              and node.y < self.app.y + self.app.height and node.y + node.height > self.app.y),
                             None)
        self.heading = next((node for node in self.exact("Find Friends")
                             if self.top_fraction(node) < 0.38), None)
        self.list_view = None
        self.list_nodes = self.nodes
        if self.heading:
            self.find_results_list(elements)
        self.count_label, self.count = self.result_count()
        self.invite, self.selected = self.invite_count()

    def find_chat_activity(self, elements):
        for chat in elements:
            if chat.get("name") != "cui.screen.chat":
                continue
            for view in chat.iter("XCUIElementTypeScrollView"):
                viewport = elements[view]
                if viewport.width != self.app.width or not 0 <= viewport.x < self.app.width:
                    continue
                events = []
                for row in view:
                    box = elements[row]
                    if (row.tag != "XCUIElementTypeOther" or len(row) != 1
                            or box.width != viewport.width or box.height > 30
                            or not viewport.y <= box.center[1] <= viewport.y + viewport.height):
                        continue
                    content = row[0]
                    if content.tag == "XCUIElementTypeOther" and len(content) == 1:
                        label = content[0]
                        if label.tag == "XCUIElementTypeStaticText" and label.get("label"):
                            events.append((box.y, label.attrib["label"]))
                if events:
                    return [label for _, label in sorted(events)]
        return []

    def invite_activity(self, actor):
        prefix = actor + " invited "
        return [label[len(prefix):] for label in self.chat_events
                if label.startswith(prefix) and USERNAME.fullmatch(label[len(prefix):])]

    def find_results_list(self, elements):
        field = self.field()
        parents = {child: node for node in elements for child in node}
        branch = next(node for node, element in elements.items() if element is field)
        while branch in parents:
            branch = parents[branch]
            lists = [node for node in branch.iter()
                     if node.tag in ("XCUIElementTypeCollectionView", "XCUIElementTypeTable")
                     and elements[node].visible and elements[node].y >= field.center[1]
                     and elements[node].width > self.app.width * 0.75]
            if len(lists) == 1:
                view = lists[0]
                self.list_view = elements[view]
                self.list_nodes = [elements[node] for node in view.iter() if elements[node].visible]
                return
            if len(lists) > 1:
                return

    def top_fraction(self, node):
        return (node.center[1] - self.app.y) / self.app.height

    def exact(self, label):
        return [node for node in self.nodes
                if node.kind in ("XCUIElementTypeButton", "XCUIElementTypeStaticText",
                                 "XCUIElementTypeOther", "XCUIElementTypeCell")
                and any(text.casefold() == label.casefold() for text in node.texts)]

    def control(self, label):
        matches = [node for node in self.exact(label) if node.enabled]
        buttons = [node for node in matches if node.kind == "XCUIElementTypeButton"]
        return unique_location(buttons or matches, label)

    def result_count(self):
        if not self.heading:
            return None, None
        labels = []
        for node in self.list_nodes:
            if node.center[1] <= self.heading.center[1]:
                continue
            for text in node.texts:
                if match := COUNT.fullmatch(text):
                    labels.append((node, int(match[1])))
                    break
        counts = {count for _, count in labels}
        if len(counts) > 1:
            raise Stop("Polymarket exposed conflicting result counts.")
        if labels:
            return labels[0]
        if any("no results found for this search" in " ".join(text.split()).casefold()
               for node in self.nodes for text in node.texts):
            return None, 0
        return None, None

    def invite_count(self):
        if not self.heading:
            return None, 0
        matches = []
        for node in self.nodes:
            x_fraction = (node.center[0] - self.app.x) / self.app.width
            if not node.enabled or not (0.78 < self.top_fraction(node) < 0.99 and 0.35 < x_fraction < 0.65):
                continue
            for text in node.texts:
                if match := INVITE_COUNT.fullmatch(text):
                    matches.append((node, int(match[1])))
                    break
        counts = {count for _, count in matches}
        if len(counts) > 1:
            raise Stop("Polymarket exposed conflicting invite counts.")
        if not matches:
            return None, len(self.selected_accounts) if self.keyboard else 0
        buttons = [pair for pair in matches if pair[0].kind == "XCUIElementTypeButton"]
        return (buttons or matches)[0]

    def field(self):
        if not self.heading:
            raise Stop("Open the Squad’s Find Friends invite picker.")
        fields = [node for node in self.nodes
                  if node.kind in ("XCUIElementTypeTextField", "XCUIElementTypeSearchField")
                  and self.heading.center[1] < node.center[1] < self.heading.center[1] + self.app.height * 0.18]
        field = unique_location(fields, "Find Friends search")
        if field is None:
            raise Stop("The Find Friends search field is missing from iPhone accessibility.")
        return field

    def bounds(self):
        top = self.field().y + self.field().height
        for label in ("Find friends from contacts", "Enable access in settings"):
            for node in self.exact(label):
                top = max(top, node.y + node.height)
        if self.count_label:
            top = max(top, self.count_label.y + self.count_label.height)
        bottom = self.app.y + self.app.height * 0.99
        if self.list_view:
            top = max(top, self.list_view.y)
            bottom = min(bottom, self.list_view.y + self.list_view.height)
        for overlay in (self.invite, self.keyboard):
            if overlay:
                bottom = min(bottom, overlay.y - 3)
        return top, bottom

    def rows(self):
        if self.count == 0:
            return {}
        top, bottom = self.bounds()
        statuses = [node for node in self.list_nodes if any(STATUS.fullmatch(text) for text in node.texts)]
        rows = {}
        for node in self.list_nodes:
            if node.kind not in ("XCUIElementTypeStaticText", "XCUIElementTypeButton", "XCUIElementTypeOther",
                                  "XCUIElementTypeCell"):
                continue
            _, y = node.center
            x_fraction = (node.x - self.app.x) / self.app.width
            in_view = top + 3 < y < bottom - 4
            clipped = (self.list_view is not None and bottom - 4 <= y <= bottom + 24
                       and id(node) in self.row_accounts)
            if not ((in_view or clipped) and 0.12 < x_fraction < 0.40):
                continue
            for text in node.texts:
                parts = re.split(r"\s*[,\n]\s*", text, maxsplit=1)
                match = USERNAME.fullmatch(parts[0])
                if not match or (len(parts) > 1 and not STATUS.fullmatch(parts[1])):
                    continue
                handle = match[1]
                status = parts[1] if len(parts) > 1 else next((
                    text for marker in statuses for text in marker.texts
                    if STATUS.fullmatch(text) and abs(marker.x - node.x) < 15
                    and 2 < marker.center[1] - y < 24
                ), None)
                row = Row(handle, node, status, self.row_accounts.get(id(node)))
                if not in_view and self.selection_point(row) is None:
                    continue
                previous = rows.get(row.key)
                if previous and abs(previous.node.center[1] - y) > 22:
                    raise Stop(f"Multiple visible rows match @{handle}.")
                if not previous or status or (not previous.status and node.kind == "XCUIElementTypeStaticText"):
                    rows[row.key] = row
                break
        return rows

    def selection_control(self, row):
        y = row.node.center[1]
        controls = [node for node in self.list_nodes
                    if node.kind in ("XCUIElementTypeButton", "XCUIElementTypeSwitch")
                    and node.enabled and abs(node.center[1] - y) <= 8
                    and (not row.account_id or self.row_accounts.get(id(node)) == row.account_id)
                    and 0.78 < (node.center[0] - self.app.x) / self.app.width < 0.98
                    and node.width < self.app.width * 0.2]
        return unique_location(controls, f"selection circle for @{row.handle}")

    def selection_point(self, row, control=None):
        control = control or self.selection_control(row)
        if control is None:
            return None
        top, bottom = self.bounds()
        x, y = control.center
        if not top < y < bottom - 4:
            # A bottom row's name and circle center can be clipped while the
            # circle's top edge still accepts taps. Require its exact cell ID.
            if (self.list_view is None or not row.account_id
                    or self.row_accounts.get(id(control)) != row.account_id
                    or row.node.center[1] < bottom - 4):
                return None
            y = round(control.y)
        if not (self.app.x < round(x) < self.app.x + self.app.width
                and top < round(y) < bottom):
            return None
        if self.invite and round(y) >= self.invite.y - 3:
            return None
        return x, y

    def results_fit_above_keyboard(self):
        rows = self.rows()
        if (not self.list_view or not rows or len(rows) != self.count
                or self.selected != len(self.selected_accounts)):
            return False
        top, bottom = self.bounds()
        for row in rows.values():
            if not row.account_id:
                return False
            if row.selectable and row.key not in self.selected_accounts:
                control = self.selection_control(row)
                if not control or not top < control.y < control.y + control.height < bottom:
                    return False
        return True

    def keyboard_return(self):
        if not self.keyboard:
            return None
        return next((node for label in ("return", "done", "search", "go")
                     for node in self.exact(label)
                     if node.kind == "XCUIElementTypeButton" and node.enabled
                     and self.keyboard.contains(node.center) and self.app.contains(node.center)), None)


class Polymarket:
    def __init__(self, driver, args):
        self.driver = driver
        self.args = args
        self._input_context = None
        self._ready_invite_route = None
        self._snapshot_seconds = 0.0
        self._allow_shallow_picker = (getattr(args, "shallow_picker_reads", False)
                                      and hasattr(driver, "set_picker_snapshot"))

    def snapshot(self):
        if getattr(self.driver, "picker_snapshot", False):
            with contextlib.suppress(Stop, ET.ParseError):
                screen = Screen(self.driver.get("/source"), self.args.bundle_id)
                if (screen.heading and screen.list_view is not None
                        and screen.selected == len(screen.selected_accounts)
                        and (not getattr(self.args, "verify_squad", False) or screen.exact(self.args.squad))):
                    return screen
            self.driver.set_picker_snapshot(False)
            self._allow_shallow_picker = False
        return Screen(self.driver.get("/source"), self.args.bundle_id)

    def screen(self):
        started = time.monotonic()
        screen = self.snapshot()
        if screen.alert and "Undo Typing" in screen.alert.texts:
            self.driver.post("/alert/dismiss", {})
            screen = self.snapshot()
        if screen.alert:
            raise Stop("Polymarket or iOS opened an alert. Read it on the phone before resuming.")
        if screen.exact("Build a Combo"):
            raise Stop("Polymarket opened Build a Combo. Return to the Squad before resuming.")
        captured_at = time.monotonic()
        self._snapshot_seconds = captured_at - started
        self._input_context = (screen, captured_at, getattr(self.driver, "ui_revision", None))
        return screen

    def search_context(self):
        # Reuse only the just-read search field as input context. Search results,
        # selections and submission checks still get fresh screen reads.
        screen = self.recent_screen(self._input_context)
        if screen and screen.heading:
            return screen
        return self.screen()

    def recent_screen(self, context):
        if context is not None:
            screen, captured_at, revision = context
            if (revision is not None and revision == getattr(self.driver, "ui_revision", None)
                    and time.monotonic() - captured_at <= 1.0):
                return screen
        return None

    def wait(self, predicate, description, timeout=10, poll_delay=None):
        deadline = time.monotonic() + timeout
        while True:
            screen = self.screen()
            if predicate(screen):
                return screen
            if time.monotonic() >= deadline:
                raise Stop(f"Timed out waiting for {description}.")
            time.sleep(poll_delay() if poll_delay else 0.10)

    def open_picker(self):
        ready = self.recent_screen(self._ready_invite_route)
        self._ready_invite_route = None
        self.check_active_app()
        screen = ready or self.screen()
        if screen.heading:
            return screen
        if not screen.exact(self.args.squad):
            raise Stop(f"Open the {self.args.squad!r} Squad on the iPhone and rerun.")
        path = self.args.open_via
        if not path:
            label = next((label for label in ("Invite friends", "Invite", "Add members")
                          if screen.control(label)), None)
            if label is None:
                raise Stop("Provide the Squad’s invite route with --open-via, one exact label per step.")
            path = [label]
        for index in reversed(range(len(path))):
            if screen.control(path[index]):
                path = path[index:]
                break
        for index, label in enumerate(path):
            if index or not screen.control(label):
                screen = self.wait(lambda current: current.control(label), repr(label))
            try:
                self.driver.tap_control(screen.control(label))
            except ControlUnavailable:
                if ready is not None and not index:
                    return self.open_picker()
                raise
        picker = self.wait(lambda current: current.heading, "the Find Friends picker")
        if ready is not None and getattr(self.args, "verify_squad", False) and not picker.exact(self.args.squad):
            raise Stop("The reopened Find Friends picker does not show the expected Squad.")
        return picker

    def check_active_app(self):
        active = self.driver.post("/execute/sync", {"script": "mobile: activeAppInfo", "args": []})
        if active.get("bundleId") != self.args.bundle_id:
            raise Stop("Open Polymarket on the connected iPhone before continuing.")

    def search(self, query):
        initial = self.search_context()
        field = initial.field()
        if self._allow_shallow_picker and initial.list_view is not None:
            self.driver.set_picker_snapshot(True)
        old_query = field.attributes.get("value", "").casefold()
        old_rows = tuple(initial.rows()) if initial.count else ()
        incremental = bool(getattr(self.args, "incremental_search", False))
        if incremental:
            edited = self.driver.change_text(field, query, incremental=True)
        else:
            edited = self.driver.change_text(field, query)
        # Find Friends debounces input and can briefly display results from the previous query.
        changed_at = time.monotonic()
        time.sleep(self.args.search_delay)
        stable = None
        blank_read_at = None
        empty_delay = getattr(self.args, "empty_search_delay", 1.0)
        observe_empty_after = getattr(self.args, "empty_observation_delay", None)
        if observe_empty_after is None:
            observe_empty_after = empty_delay

        def next_poll():
            if blank_read_at is not None:
                return max(0.10, blank_read_at - time.monotonic() - self._snapshot_seconds)
            return 0.10

        def ready(screen):
            nonlocal stable, blank_read_at
            blank_read_at = None
            if not screen.heading or screen.field().attributes.get("value", "").casefold() != query.casefold():
                stable = None
                return False
            if screen.count is None:
                stable = None
                return False
            # An early blank can start a check, but only a later matching
            # frame can confirm the query has no results.
            observed_at = time.monotonic()
            if screen.count == 0 and observed_at < changed_at + observe_empty_after:
                blank_read_at = changed_at + observe_empty_after
                stable = None
                return False
            rows = screen.rows()
            keys = tuple(rows)
            if (old_query != query.casefold() and keys and keys == old_rows
                    and not any(query.casefold() in row.handle.casefold() for row in rows.values())):
                stable = None
                return False
            signature = (screen.count, keys)
            matched = signature == stable
            stable = signature
            if screen.count == 0 and observed_at < changed_at + empty_delay:
                blank_read_at = changed_at + empty_delay
                return False
            return matched

        if edited:
            initial = self.screen()
            if initial.field().attributes.get("value", "").casefold() == query.casefold():
                ready(initial)
                time.sleep(next_poll())
            else:
                self.driver.change_text(initial.field(), query)
                changed_at = time.monotonic()
                time.sleep(self.args.search_delay)
        screen = self.wait(ready, f"settled search results for {query!r}", timeout=16,
                           poll_delay=next_poll)
        if screen.count and screen.keyboard and not screen.results_fit_above_keyboard():
            return self.dismiss_keyboard(screen, query)
        return screen

    def dismiss_keyboard(self, screen, query):
        if screen.keyboard:
            key = screen.keyboard_return()
            if key:
                self.driver.tap(*key.center)
            else:
                with contextlib.suppress(Stop):
                    self.driver.post("/execute/sync", {"script": "mobile: hideKeyboard", "args": []})
            screen = self.screen()
            if not screen.heading or screen.field().attributes.get("value", "").casefold() != query.casefold():
                raise Stop(f"Find Friends changed while hiding the keyboard for {query!r}.")
            if screen.keyboard:
                self.driver.tap(*screen.heading.center)
                screen = self.screen()
                if screen.keyboard or not screen.heading:
                    raise Stop("The iPhone keyboard covered the results and could not be dismissed.")
        return screen

    def collect(self, query, screen=None, on_page=None, needs_selection=None, scroll_fraction=None,
                target_accounts=None, on_covered=None):
        if target_accounts is not None and (on_page or needs_selection or on_covered):
            raise ValueError("Target-limited collection is only for read-only verification.")
        screen = screen or self.search(query)
        if screen.count is None:
            raise Stop(f"Result count disappeared for {query!r}.")
        expected = screen.count
        fraction = scroll_fraction or getattr(self.args, "scroll_fraction", None) or 1.0
        recognized_list = screen.list_view is not None
        fast_scroll = fraction > 0.43 and recognized_list
        found = screen.rows()
        def targets_done():
            return target_accounts is not None and all(
                account in found and found[account].invited for account in target_accounts
            )
        if on_page:
            screen = on_page(screen)
            found.update(screen.rows())
        for _ in range(expected * 2):
            if targets_done():
                break
            if len(found) >= expected and not (needs_selection and needs_selection(found)):
                break
            if recognized_list and screen.list_view is None:
                break
            top, bottom = screen.bounds()
            span = bottom - top
            if span < 120:
                break
            previous_rows = tuple(screen.rows())
            previous_selection = screen.selected
            x = screen.app.x + screen.app.width * 0.60
            if fast_scroll:
                start_y = bottom - 2 if fraction == 1.0 else top + span * 0.86
                end_y = top + 2 if fraction == 1.0 else top + span * (0.86 - fraction)
                self.driver.touch(x, start_y, end_y,
                                  drag_ms=getattr(self.args, "fast_swipe_drag_ms", 250),
                                  hold_ms=getattr(self.args, "fast_swipe_hold_ms", 80))
                settle = getattr(self.args, "fast_swipe_settle", 0.08)
                if settle:
                    time.sleep(settle)
            else:
                self.driver.touch(x, top + span * 0.85, top + span * 0.42)
                time.sleep(0.30)
            screen = self.screen()
            if not screen.heading or screen.field().attributes.get("value", "").casefold() != query.casefold():
                raise Stop(f"Find Friends changed while scrolling {query!r}.")
            visible_rows = screen.rows()
            scrolled = tuple(visible_rows) != previous_rows
            new_accounts = visible_rows.keys() - found.keys()
            found.update(visible_rows)
            if on_covered is not None and len(found) == expected and needs_selection and needs_selection(found):
                attempt_group, on_covered = on_covered, None
                if attempt_group(screen):
                    if not needs_selection(found):
                        return expected, found
                    screen = self.search(query)
                    if screen.count != expected:
                        return self.collect(query, screen=screen, on_page=on_page,
                                            needs_selection=needs_selection, scroll_fraction=scroll_fraction)
                    continue
            if on_page:
                screen = on_page(screen)
            next_rows = screen.rows()
            new_accounts |= next_rows.keys() - found.keys()
            found.update(next_rows)
            # Selecting a result can reset the list to its top. Continue with
            # full-height drags while selections advance, rather than treating
            # that normal reset as incomplete coverage requiring shorter drags.
            if fast_scroll and fraction == 1.0 and screen.selected == previous_selection:
                break
            if not scrolled and screen.selected == previous_selection and not new_accounts:
                break
        if fast_scroll and not targets_done() and (len(found) < expected or needs_selection and needs_selection(found)):
            count, conservative = self.collect(query, on_page=on_page, needs_selection=needs_selection,
                                               scroll_fraction=0.43, target_accounts=target_accounts)
            if count == expected:
                found.update(conservative)
        return expected, found

    def matching_row(self, screen, handle, account_id):
        rows = screen.rows()
        if account_id and account_id.startswith(ACCOUNT_ROW):
            return rows.get(account_id)
        matches = [row for row in rows.values() if row.handle.casefold() == handle.casefold()]
        return matches[0] if len(matches) == 1 else None

    def exact_row(self, handle, account_id=None):
        screen = self.search(handle)
        row = self.matching_row(screen, handle, account_id)
        if row is None:
            raise Stop(f"Could not verify @{handle} in an exact search.")
        return screen, row

    def wait_for_invited(self, handle, account_id=None):
        screen, row = self.exact_row(handle, account_id)
        if screen.selected:
            return False
        if row.invited:
            return True
        try:
            self.wait(lambda current: not current.selected
                      and current.field().attributes.get("value", "").casefold() == handle.casefold()
                      and (match := self.matching_row(current, handle, account_id)) and match.invited,
                      f"Polymarket to confirm @{handle}", timeout=8)
        except Stop:
            return False
        return True

    def selection_timing(self, screen):
        if not getattr(screen, "keyboard", None) and hasattr(self.driver, "selection_timing"):
            return self.driver.selection_timing()
        return contextlib.nullcontext(False)

    def select(self, screen, row, expected):
        if screen.selected != expected or expected >= BATCH_CAP or not row.selectable:
            raise Stop("The current selection differs from the planned batch.")
        control = screen.selection_control(row)
        if control is None or control.attributes.get("value") == "1" or row.key in screen.selected_accounts:
            raise Stop(f"Could not identify an unselected circle for @{row.handle}.")
        point = screen.selection_point(row, control)
        if point is None:
            raise Stop(f"No exposed selection point for @{row.handle}.")
        with_keyboard = bool(screen.keyboard and row.account_id)
        query = screen.field().attributes.get("value") if with_keyboard else None
        before = screen.selected_accounts
        if with_keyboard and len(before) != expected:
            raise Stop("The keyboard-visible selection does not match the planned batch.")
        def selected(current):
            if not current.heading or current.selected != expected + 1:
                return False
            if with_keyboard:
                return (current.field().attributes.get("value") == query
                        and current.selected_accounts == before | {row.key})
            if row.account_id in current.selected_accounts:
                return True
            chosen = current.rows().get(row.key)
            circle = current.selection_control(chosen) if chosen else None
            return circle is not None and circle.attributes.get("value") == "1"

        with self.selection_timing(screen):
            self.driver.tap(*point)
            if not with_keyboard:
                return self.wait(selected, f"selection of @{row.handle}", timeout=10)

        def unchanged(current):
            return (current.heading and current.field().attributes.get("value") == query
                    and current.selected == expected and current.selected_accounts == before)

        current = self.screen()
        if selected(current):
            return current
        if unchanged(current):
            # Some iOS layouts consume the first tap to dismiss the keyboard.
            # Check again after dismissal before tapping the same account.
            current = self.dismiss_keyboard(current, query) if current.keyboard else self.screen()
            if selected(current):
                return current
            if unchanged(current) and not current.keyboard and (live_row := current.rows().get(row.key)):
                return self.select(current, live_row, expected)
        raise Stop(f"The keyboard-visible tap changed an unexpected selection for @{row.handle}.")

    def select_group(self, screen, rows, expected):
        """Try one touch event, then verify and repair the exact selections."""
        before = set(screen.selected_accounts)
        targets = {row.key for row in rows}
        if (not screen.heading or screen.selected != expected or len(before) != expected
                or not 2 <= len(rows) <= 10 or len(targets) != len(rows)
                or expected + len(rows) > BATCH_CAP or before & targets):
            raise Stop("The rapid selection group does not match the current selection.")
        points = []
        for row in rows:
            control = screen.selection_control(row)
            if (not row.account_id or not row.account_id.startswith(ACCOUNT_ROW)
                    or not row.selectable or control is None or control.attributes.get("value") == "1"):
                raise Stop(f"Cannot identify an unselected account for @{row.handle}.")
            point = screen.selection_point(row, control)
            if point is None:
                raise Stop(f"No exposed selection point for @{row.handle}.")
            points.append(point)
        if len(set(points)) != len(points):
            raise Stop("Rapid selection targets share a tap position.")
        query = screen.field().attributes.get("value")
        shortened_wait = False
        if getattr(self.args, "independent_touch_events", True):
            with self.selection_timing(screen) as shortened_wait:
                self.driver.tap_event(points)
                current = self.screen()
        else:
            self.driver.tap_sequence(points)
            current = self.screen()
        wanted = before | targets
        if shortened_wait and before <= current.selected_accounts < wanted:
            # The event can return before the last chips finish appearing.
            current = self.screen()
        if (not current.heading or current.field().attributes.get("value") != query
                or current.selected != len(current.selected_accounts)
                or not before <= current.selected_accounts <= wanted):
            raise Stop("The rapid taps changed an unexpected selection or screen.")
        # Retry only missing accounts, using fresh geometry and the normal check.
        for row in rows:
            if row.key not in current.selected_accounts:
                live_row = current.rows().get(row.key)
                if live_row is None:
                    raise Stop(f"Missing rapid selection @{row.handle} is no longer exposed.")
                current = self.select(current, live_row, current.selected)
        if current.selected_accounts != wanted or current.selected != len(wanted):
            raise Stop("The rapid selection group could not be confirmed.")
        return current

    def submit(self, handles):
        if getattr(self.driver, "picker_snapshot", False):
            self.driver.set_picker_snapshot(False)
        self.check_active_app()
        screen = self.screen()
        if screen.keyboard:
            screen = self.dismiss_keyboard(screen, screen.field().attributes.get("value", ""))
        if getattr(self.args, "verify_squad", False) and not screen.exact(self.args.squad):
            raise Stop(f"The invite picker no longer shows the expected Squad {self.args.squad!r}.")
        if (not screen.invite or screen.invite.kind != "XCUIElementTypeButton"
                or screen.selected != len(handles) or not 1 <= screen.selected <= BATCH_CAP):
            raise Stop("The final Invite button does not match the planned batch.")
        actor = getattr(self.args, "invite_actor", None)
        previous = screen.invite_activity(actor) if actor else []
        self.driver.tap_control(screen.invite)
        ready = self.wait_for_invite_route()
        self._ready_invite_route = (ready, time.monotonic(), getattr(self.driver, "ui_revision", None))
        verified = self.verify_chat_activity(actor, previous, handles) if previous else set()
        return verified

    def wait_for_invite_route(self):
        def ready_to_reopen(current):
            # A cleared count can precede dismissal. Require the named Squad and
            # a control that iOS says can actually be tapped through the animation.
            if current.heading or not current.exact(self.args.squad):
                return False
            route = self.args.open_via
            # Submission may return to the invite menu above the Squad chat.
            # Match the deepest available route step, just as open_picker does.
            labels = reversed(route) if route else ("Invite friends", "Invite", "Add members")
            return any((control := current.control(label)) and self.driver.control_hittable(control)
                       for label in labels)

        return self.wait(ready_to_reopen, "the target Squad’s invite route to be ready", timeout=18)

    def verify_chat_activity(self, actor, previous, handles):
        anchor = previous[-1]
        targets = {handle for handle, count in Counter(handles).items() if count == 1} - set(previous)
        if not targets or previous.count(anchor) != 1:
            return set()
        screen = self.screen()
        if screen.heading or not screen.exact(self.args.squad):
            return set()
        events = screen.invite_activity(actor)
        if events.count(anchor) != 1:
            return set()
        return targets.intersection(events[events.index(anchor) + 1:])


def alphabet_key(value, alphabet):
    ranks = {character: index for index, character in enumerate(alphabet)}
    return tuple((ranks.get(character, len(ranks)), character) for character in value.casefold())


class Journal:
    def __init__(self, args):
        self.path = args.state
        self.identity = {"prefix": args.prefix, "squad": args.squad, "alphabet": args.alphabet}
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
            previous = self.data.get("identity")
            if (self.data.get("version") == 1 and isinstance(previous, dict)
                    and previous != self.identity
                    and previous | {"alphabet": args.alphabet} == self.identity
                    and set(previous.get("alphabet", "")) == set(args.alphabet)
                    and len(previous.get("alphabet", "")) == len(args.alphabet)):
                # An order-only change preserves coverage and submitted accounts.
                self.data["identity"] = self.identity.copy()
                self.data["remaining"].sort(key=lambda query: alphabet_key(query, args.alphabet), reverse=True)
                self.save()
            if self.data.get("identity") != self.identity or self.data.get("version") != 1:
                raise Stop("This state file belongs to a different Squad, prefix, or program version.")
        else:
            self.data = {
                "version": 1, "identity": self.identity, "remaining": [args.prefix], "searched": 0,
                "candidates": {}, "unresolved": {}, "confirmed": [], "pending": [],
            }
        self.data.setdefault("unavailable", {})
        self.data.setdefault("origins", {})

    def save(self):
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n")
        temporary.replace(self.path)


def continuation_suffixes(query, count, rows, alphabet, ordered):
    if not ordered or count != RESULT_CAP or len(rows) != count:
        return alphabet
    handles = [row.handle.casefold() for row in rows.values()]
    if handles != sorted(handles) or any(not handle.startswith(query) for handle in handles):
        return alphabet
    cutoff = handles[-1][len(query):len(query) + 1]
    if not cutoff.isalnum() or cutoff not in alphabet:
        return alphabet
    # Keep the cutoff child for accounts that share its first character.
    # Probe punctuation separately because its position is less predictable.
    return "".join(char for char in alphabet if not char.isalnum() or char >= cutoff)


def discover(market, journal, batch=None, ordered=False, stop_after=None):
    data = journal.data
    alphabet = journal.identity["alphabet"]
    root = journal.identity["prefix"]
    remaining = data["remaining"]
    while remaining:
        query = remaining[-1]
        if stop_after and not query.startswith(stop_after):
            return
        count, rows = batch.collect(query) if batch else market.collect(query)
        matching = {key: row for key, row in rows.items() if row.handle.casefold().startswith(query)}
        for key, row in matching.items():
            if row.selectable:
                data["candidates"][key] = row.handle
                data["unavailable"].pop(key, None)
                data["origins"].setdefault(key, query)
            elif row.status and not row.invited:
                data["unavailable"][key] = {"handle": row.handle, "status": row.status}
        remaining.pop()
        if len(rows) < count:
            data["unresolved"][query] = f"Read {len(rows)} of {count} displayed results."
        if len(query) == 1 and count == 0:
            # Find Friends can suppress single-character searches.
            remaining.extend(query + character for character in reversed(alphabet))
            print(f"{query}: no single-character results; queued {len(alphabet)} two-character searches.",
                  flush=True)
        elif count >= RESULT_CAP or len(rows) < count:
            if len(query) < 64 and (matching or count >= RESULT_CAP):
                suffixes = continuation_suffixes(query, count, rows, alphabet, ordered)
                remaining.extend(query + character for character in reversed(suffixes))
                skipped = len(alphabet) - len(suffixes)
                if skipped:
                    data["pruned"] = data.get("pruned", 0) + skipped
                    print(f"{query}: skipped {skipped} child prefixes before the last visible username.", flush=True)
            else:
                data["unresolved"][query] = "The search could not be split and verified."
        data["searched"] += 1
        journal.save()
        if count >= RESULT_CAP or data["searched"] % 10 == 0:
            print(f"{query}: {count} results. {data['searched']} searches, "
                  f"{len(data['candidates'])} candidates, {len(remaining)} prefixes queued.", flush=True)
        if stop_after and not any(prefix.startswith(stop_after) for prefix in remaining):
            return
        if batch:
            batch.flush_if_due()
    print(f"Discovery finished for {root!r}. {len(data['candidates'])} invite candidates "
          f"from {data['searched']} searches.", flush=True)
    if data["unresolved"]:
        print(f"{len(data['unresolved'])} searches had incomplete coverage. See the state file.", flush=True)


def accept_pending_batch(journal):
    """User policy: one confirmed invitation is sufficient for the whole batch."""
    data = journal.data
    count = len(data["pending"])
    confirmed = set(data["confirmed"])
    for candidate in data["pending"]:
        account = candidate.casefold() if isinstance(candidate, str) else candidate["account"]
        if account not in confirmed:
            data["confirmed"].append(account)
            confirmed.add(account)
    data["pending"] = []
    journal.save()
    return count


def verify_batch(market, journal):
    data = journal.data
    for candidate in data["pending"]:
        handle = candidate if isinstance(candidate, str) else candidate["handle"]
        account = handle.casefold() if isinstance(candidate, str) else candidate["account"]
        if market.wait_for_invited(handle, account):
            count = accept_pending_batch(journal)
            print(f"Accepted all {count} submitted invites based on confirmation of @{handle}.", flush=True)
            return
    if data["pending"]:
        raise Stop("Polymarket has not confirmed any invite in the batch. The pending batch is saved.")


def record_audited(journal, audited):
    data = journal.data
    verified = [candidate for candidate in data["pending"]
                if isinstance(candidate, dict) and candidate["handle"] in audited]
    if verified:
        accept_pending_batch(journal)
    return len(verified)


def submit_batch(market, journal, batch):
    print(f"Submitting {len(batch)} invite(s) to {journal.identity['squad']}: "
          + ", ".join(f"@{candidate['handle']}" for candidate in batch), flush=True)
    journal.data["pending"] = batch.copy()
    journal.save()
    audited = market.submit([candidate["handle"] for candidate in batch])
    if audited:
        verified = record_audited(journal, audited)
        print(f"Confirmed {verified} from Squad activity; accepted all {len(batch)} submitted invites.", flush=True)
    market.open_picker()
    verify_batch(market, journal)
    print(f"Accepted the batch. {len(journal.data['confirmed'])} accounts recorded overall.", flush=True)


class VisibleBatch:
    def __init__(self, market, journal, batch_size):
        self.market = market
        self.journal = journal
        self.batch_size = batch_size
        self.entries = {}
        self.started_at = None

    def collect(self, query):
        screen = self.market.search(query)
        if screen.count is None or not 0 < screen.count <= RESULT_CAP:
            return self.market.collect(query, screen=screen)
        room = self.batch_size - len(self.entries)
        if self.entries and screen.count > room:
            visible = screen.rows()
            if len(visible) != screen.count or sum(self.eligible(row, query) for row in visible.values()) > room:
                self.flush()
                screen = self.market.search(query)
        if screen.count is not None and 0 < screen.count <= RESULT_CAP:
            first_page = True

            def select_page(page):
                nonlocal first_page
                allow_group = first_page
                first_page = False
                return self.select_page(page, query, allow_group=allow_group)

            return self.market.collect(
                query, screen=screen,
                on_page=select_page,
                on_covered=lambda page: self.try_narrow_group(page, query),
                needs_selection=lambda rows: len(self.entries) < self.batch_size and any(
                    self.eligible(row, query) for row in rows.values()
                ),
            )
        return self.market.collect(query, screen=screen)

    def eligible(self, row, query):
        return (row.selectable and row.handle.casefold().startswith(query)
                and row.key not in self.journal.data["confirmed"] and row.key not in self.entries)

    def select_rows(self, screen, rows, query):
        data = self.journal.data
        for row in rows:
            data["candidates"][row.key] = row.handle
            data["origins"][row.key] = query
        self.journal.save()
        if len(rows) == 1:
            screen = self.market.select(screen, rows[0], len(self.entries))
        else:
            screen = self.market.select_group(screen, rows, len(self.entries))
        for row in rows:
            self.entries[row.key] = {"handle": row.handle, "account": row.key, "query": query}
        if self.started_at is None:
            self.started_at = time.monotonic()
        return screen

    def group_rows(self, screen, query, limit):
        return [row for row in screen.rows().values() if self.eligible(row, query)
                and row.account_id and screen.selection_point(row) is not None][:limit]

    def at_top(self, screen):
        return (screen.list_view is not None and screen.count_label is not None and not screen.keyboard
                and screen.list_view.y <= screen.count_label.center[1] < screen.bounds()[1]
                and screen.selected_accounts == self.entries.keys()
                and screen.selected == len(self.entries))

    def select_page(self, screen, query, allow_group=False):
        if allow_group and self.entries and self.at_top(screen):
            group = self.group_rows(screen, query, min(10, self.batch_size - len(self.entries)))
            if len(group) > 1:
                screen = self.select_rows(screen, group, query)
        while len(self.entries) < self.batch_size:
            if screen.selected != len(self.entries):
                raise Stop("The app’s selected count changed unexpectedly during discovery.")
            visible = screen.rows()
            first_account = next(iter(visible), None)
            row = next((item for item in visible.values() if self.eligible(item, query)), None)
            if row is None:
                break
            screen = self.select_rows(screen, [row], query)
            # A bottom-page tap normally resets to different first rows.
            if (screen.list_view is not None and first_account == next(iter(screen.rows()), None)
                    and screen.selected_accounts == self.entries.keys()):
                group = self.group_rows(screen, query, min(9, self.batch_size - len(self.entries)))
                if len(group) > 1:
                    screen = self.select_rows(screen, group, query)
        return screen

    def try_narrow_group(self, screen, query):
        # Only try when enough lower-page taps can amortize the extra search.
        minimum = 3
        room = min(10, self.batch_size - len(self.entries))
        if room < minimum or not self.entries or screen.list_view is None:
            return False
        groups = {}
        for row in screen.rows().values():
            if not row.account_id or not self.eligible(row, query):
                continue
            handle = row.handle.casefold()
            for end in range(len(query) + 1, len(handle) + 1):
                groups.setdefault(handle[:end], []).append(row.key)
        child, accounts = max(groups.items(), key=lambda pair: (min(room, len(pair[1])), len(pair[0])),
                              default=("", []))
        if len(accounts) < minimum:
            return False
        accounts = accounts[:room]
        narrowed = self.market.search(child)
        visible = narrowed.rows()
        if narrowed.keyboard and all(key in visible for key in accounts):
            narrowed = self.market.dismiss_keyboard(narrowed, child)
            visible = narrowed.rows()
        targets = [visible[key] for key in accounts if key in visible
                   and self.eligible(visible[key], query) and narrowed.selection_point(visible[key]) is not None]
        if self.at_top(narrowed) and len(targets) == len(accounts):
            self.select_rows(narrowed, targets, child)
        # The caller restores the parent if any known rows still need selection.
        return True

    def flush_if_due(self):
        if len(self.entries) >= self.batch_size or self.started_at is not None and time.monotonic() - self.started_at >= 300:
            self.flush()

    def flush(self):
        if self.entries:
            submit_batch(self.market, self.journal, list(self.entries.values()))
            self.entries.clear()
            self.started_at = None


def send(market, journal, batch_size):
    data = journal.data
    screen = market.open_picker()
    if screen.selected:
        raise Stop(f"Find Friends already has {screen.selected} selections. Clear them before starting.")
    if data["pending"]:
        print(f"Verifying {len(data['pending'])} invites from the previous run.", flush=True)
        verify_batch(market, journal)

    confirmed = set(data["confirmed"]) | data["unavailable"].keys()
    handles = sorted(
        ((handle, account) for account, handle in data["candidates"].items() if account not in confirmed),
        key=lambda entry: (alphabet_key(entry[0], journal.identity["alphabet"]), entry[1]),
    )
    while handles:
        batch = []
        while handles and len(batch) < batch_size:
            handle, account = handles.pop(0)
            screen, row = market.exact_row(handle, account)
            if screen.selected != len(batch):
                raise Stop("The app’s selected count changed unexpectedly.")
            if row.invited:
                data["confirmed"].append(account)
                journal.save()
                continue
            if row.status:
                data["unavailable"][account] = {"handle": handle, "status": row.status}
                journal.save()
                continue
            market.select(screen, row, len(batch))
            batch.append({"handle": handle, "account": account})
        if batch:
            submit_batch(market, journal, batch)
    print(f"Confirmed {len(data['confirmed'])} accounts. "
          f"Polymarket marked {len(data['unavailable'])} accounts unavailable.", flush=True)


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.set_defaults(fast_swipe_drag_ms=150, fast_swipe_hold_ms=60, fast_swipe_settle=0,
                        verify_squad=True)
    parser.add_argument("prefix", help="Username prefix, such as matt")
    parser.add_argument("--squad", required=True, help="Exact Squad name")
    parser.add_argument("--udid", required=True, help="The connected iPhone’s device identifier")
    parser.add_argument("--server", default="http://127.0.0.1:4723", help="Local Appium server")
    parser.add_argument("--bundle-id", default="com.polymarket.ios-app")
    parser.add_argument("--team-id", help="Apple Developer team identifier for Appium-managed signing")
    parser.add_argument("--wda-bundle-id", help="Provisioned WebDriverAgent bundle ID")
    parser.add_argument("--wda-url", help="Existing local WebDriverAgent URL")
    parser.add_argument("--wda-derived-data", type=Path, help="Reuse WebDriverAgent’s local Xcode build directory")
    parser.add_argument("--open-via", action="append", help="Exact button label. Repeat for a multi-step route.")
    parser.add_argument("--from-picker", action="store_true", help="Confirm the open Find Friends picker is in this Squad")
    parser.add_argument("--send", action="store_true", help="Automatically submit discovered invites")
    parser.add_argument("--batch-size", type=int, default=BATCH_CAP, help="Invite limit per batch, up to 80")
    parser.add_argument("--alphabet", default=DEFAULT_ALPHABET,
                        help="Characters to append in search order (default: digits, dot, underscore, letters, hyphen)")
    parser.add_argument("--ordered-results", action=argparse.BooleanOptionalAction, default=True,
                        help="Skip earlier prefixes when the capped page passes the ordering check")
    parser.add_argument("--invite-actor", help="Your exact Polymarket username for verification against Squad invite activity")
    parser.add_argument("--incremental-search", action=argparse.BooleanOptionalAction, default=True,
                        help="Edit only the search field’s changing suffix, with verified full-replacement fallback")
    parser.add_argument("--scroll-fraction", type=float, default=1.0,
                        help="Result-list drag fraction: 1.0 (default) or 0.43–0.68")
    parser.add_argument("--independent-touch-events", action=argparse.BooleanOptionalAction, default=True,
                        help="Use one touch event per stable group, then verify and repair missed selections")
    parser.add_argument("--shallow-picker-reads", action="store_true",
                        help="Try shallower Find Friends snapshots, with full-depth fallback")
    parser.add_argument("--search-delay", type=float, default=0.15, help="Initial search wait; stable results are still required")
    parser.add_argument("--empty-search-delay", type=float, default=1.15,
                        help="Minimum seconds after input before confirming stable empty results")
    parser.add_argument("--empty-observation-delay", type=float, default=0.75,
                        help="Earlier time to start observing empty results when timing has been calibrated")
    parser.add_argument("--state", type=Path, help="JSON checkpoint and discovery report")
    parser.add_argument("--inspect", action="store_true", help="Print the current accessibility XML and exit")
    args = parser.parse_args()
    args.prefix = args.prefix.casefold()
    if not USERNAME.fullmatch(args.prefix) or args.prefix.startswith("@"):
        parser.error("prefix must start with a letter or digit and contain only letters, digits, _, . or -")
    if not args.alphabet or len(set(args.alphabet)) != len(args.alphabet) or any(
        character not in DEFAULT_ALPHABET for character in args.alphabet
    ):
        parser.error("alphabet must use unique characters from " + DEFAULT_ALPHABET)
    if not 1 <= args.batch_size <= BATCH_CAP:
        parser.error("batch size must be between 1 and 80")
    if not 0.15 <= args.search_delay <= 30:
        parser.error("search delay must be between 0.15 and 30 seconds")
    if not 1.0 <= args.empty_search_delay <= 30:
        parser.error("empty search delay must be between 1.0 and 30 seconds")
    if args.empty_observation_delay is not None and not 0.45 <= args.empty_observation_delay <= args.empty_search_delay:
        parser.error("empty observation delay must be at least 0.45 seconds and at most the confirmation delay")
    if args.scroll_fraction is not None and not (0.43 <= args.scroll_fraction <= 0.68 or args.scroll_fraction == 1.0):
        parser.error("scroll fraction must be 1.0 or between 0.43 and 0.68")
    if args.team_id and not args.wda_bundle_id:
        parser.error("--team-id requires --wda-bundle-id")
    if args.inspect and args.send:
        parser.error("--inspect and --send are mutually exclusive")
    if args.state is None:
        safe_squad = re.sub(r"[^a-z0-9]+", "-", args.squad.casefold()).strip("-") or "squad"
        args.state = Path(f"squad-invites-{safe_squad}-{args.prefix}.json")
    return args


def main():
    args = arguments()
    driver = None
    try:
        if args.inspect:
            driver = Appium(args)
            print(driver.get("/source"))
            return 0
        args.state.parent.mkdir(parents=True, exist_ok=True)
        with args.state.with_suffix(args.state.suffix + ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise Stop("Another process is already using this Squad’s state file.") from error
            journal = Journal(args)
            driver = Appium(args)
            market = Polymarket(driver, args)
            if args.send and market.screen().heading and not args.from_picker:
                raise Stop("Start from the named Squad chat or use --from-picker for its already-open invite picker.")
            screen = market.open_picker()
            if args.send and screen.selected:
                raise Stop(f"Clear the {screen.selected} existing selections before running with --send.")
            batch = None
            if args.send:
                if journal.data["pending"]:
                    verify_batch(market, journal)
                batch = VisibleBatch(market, journal, args.batch_size)
            discover(market, journal, batch, ordered=args.ordered_results)
            if args.send:
                batch.flush()
                send(market, journal, args.batch_size)
            print(f"State and results: {args.state.resolve()}", flush=True)
        return 0
    except (Stop, OSError, ValueError, KeyError, ET.ParseError) as error:
        print(f"Stopped: {error}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Rerun with the same state file to resume.", file=sys.stderr)
        return 130
    finally:
        if driver is not None:
            with contextlib.suppress(Stop):
                driver.close()


if __name__ == "__main__":
    sys.exit(main())
