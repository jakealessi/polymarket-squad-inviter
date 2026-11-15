"""Run the named Polymarket squads in order, with separate saved checkpoints."""

import argparse
import contextlib
import fcntl
import json
import re
import subprocess
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from squad_inviter import (Appium, BATCH_CAP, DEFAULT_ALPHABET, USERNAME, Element, Journal,
                           Polymarket, Screen, Stop, VisibleBatch, discover, send, verify_batch)

BUNDLE = "com.polymarket.ios-app"
CHAT = "cui.screen.chat"
DIRECTORY = "cui.screen.squads"
SETTINGS = "cui.screen.squad-settings"
INVITE_MENU = "Invite to Squad"
BACK = "cui.iconButton.back"
SQUADS_TAB = "cui.tabBar.item.squads"
SCROLL_TYPES = {"XCUIElementTypeCollectionView", "XCUIElementTypeScrollView", "XCUIElementTypeTable"}
TITLE_TYPES = {"XCUIElementTypeOther", "XCUIElementTypeStaticText", "XCUIElementTypeButton"}
MISSING = 20
AMBIGUOUS = 21


@dataclass(frozen=True)
class Target:
    key: str
    prefix: str
    squad: str
    state: Path


def normalized(title):
    text = unicodedata.normalize("NFKC", title).casefold().replace("’", "'")
    return " ".join(text.split())


def current_title(screen, expected_title):
    if not screen.exact(CHAT) or screen.exact(SETTINGS):
        return None
    names = set()
    for node in screen.nodes:
        if node.kind not in TITLE_TYPES or not 0.055 < screen.top_fraction(node) < 0.145:
            continue
        for title in node.texts:
            if normalized(title) == normalized(expected_title):
                names.add(title)
    if len(names) > 1:
        raise Stop("Several squad titles are visible in the chat header")
    return next(iter(names), None)


def is_directory(screen):
    return bool(screen.exact(DIRECTORY) and not screen.heading and not screen.exact(CHAT)
                and not screen.exact(SETTINGS) and not screen.exact(INVITE_MENU))


def tap_named(market, screen, name):
    control = screen.control(name)
    if control is None:
        raise Stop(f"The expected navigation control {name!r} is missing")
    market.driver.tap_control(control)


def close_settings(market, screen):
    if not screen.exact(SETTINGS):
        return screen
    tap_named(market, screen, BACK)
    return market.wait(lambda view: not view.exact(SETTINGS), "squad settings to close")


def go_to_directory(market, screen):
    screen = close_settings(market, screen)
    if screen.heading:
        if screen.selected or screen.selected_accounts:
            raise Stop("A different squad picker has staged selections")
        tap_named(market, screen, "Back")
        screen = market.wait(lambda view: not view.heading, "Find Friends to close")
    if screen.exact(INVITE_MENU):
        tap_named(market, screen, "cui.iconButton.close")
        screen = market.wait(lambda view: not view.exact(INVITE_MENU), "the invite menu to close")
    if is_directory(screen):
        return
    tap_named(market, screen, BACK if screen.exact(CHAT) else SQUADS_TAB)
    market.wait(is_directory, "the Squads directory")


class DirectoryPage:
    def __init__(self, xml):
        screen = Screen(xml, BUNDLE)
        if not is_directory(screen) or screen.alert:
            raise Stop("The verified Squads directory is no longer visible")
        root = ET.fromstring(xml)
        scope = next((node for node in root.iter() if node.get("name") == DIRECTORY), None)
        if scope is None:
            raise Stop("The Squads directory accessibility container is missing")
        views = [(Element(node.tag, node.attrib), node) for node in scope.iter() if node.tag in SCROLL_TYPES]
        views = [(view, node) for view, node in views if view.visible and
                 view.width > 0.8 * screen.app.width and view.height > 0.4 * screen.app.height]
        if not views:
            raise Stop("Could not identify the vertical Squads list")
        view, container = max(views, key=lambda pair: pair[0].height)
        bar = screen.control("cui.tabBar.item.squads")
        self.top = max(view.y, screen.app.y)
        self.bottom = min(view.y + view.height, bar.y - 5 if bar else screen.app.y + screen.app.height - 20)
        self.x = screen.app.x + 0.58 * screen.app.width
        if self.bottom - self.top < 250:
            raise Stop("The Squads list is too small to scan safely")
        self.rows = []
        self.fingerprint = []
        self.header_y = None
        for cell in container:
            if cell.tag != "XCUIElementTypeCell":
                continue
            frame = Element(cell.tag, cell.attrib)
            if frame.y + frame.height <= self.top or frame.y >= self.bottom:
                continue
            if any(child.get("name") == "cui.button.squad-invites" for child in cell.iter()):
                self.header_y = frame.y + frame.height - 2
            titles = []
            for child in cell.iter():
                label = child.get("label")
                if not label or child.tag not in TITLE_TYPES:
                    continue
                node = Element(child.tag, child.attrib)
                if (node.enabled and node.visible and 0.16 < (node.x - screen.app.x) / screen.app.width < 0.45
                        and 12 <= node.y - frame.y <= 36):
                    titles.append((label, node))
            title, node = min(titles, key=lambda pair: pair[1].y, default=("", None))
            self.fingerprint.append((round(frame.y), round(frame.height), title))
            if node and self.top + 5 < node.center[1] < self.bottom - 5:
                self.rows.append((title, node))
        self.fingerprint = tuple(self.fingerprint)


def pages(driver, direction):
    previous = None
    stationary = 0
    for _ in range(65):
        page = DirectoryPage(driver.get("/source"))
        yield page
        stationary = stationary + 1 if page.fingerprint == previous else 0
        if stationary >= 2:
            return
        previous = page.fingerprint
        distance = (page.bottom - page.top) * 0.56
        start = (page.top + page.bottom + direction * distance) / 2
        driver.touch(page.x, start, start - direction * distance, drag_ms=430, hold_ms=80)
        time.sleep(0.16)
    raise Stop("The Squads directory scan did not reach its end")


class MissingSquad(Stop):
    pass


class AmbiguousSquad(Stop):
    pass


def directory_target(market, expected_title):
    # Back navigation can reopen the list at a previous scroll position.
    for _ in pages(market.driver, -1):
        pass
    title = None
    in_memberships = False
    for page in pages(market.driver, 1):
        if page.header_y is not None:
            in_memberships = True
        if not in_memberships:
            continue
        matches = [title for title, node in page.rows
                   if (page.header_y is None or node.center[1] > page.header_y)
                   and normalized(title) == normalized(expected_title)]
        if len(matches) > 1 or (matches and title and title != matches[0]):
            raise AmbiguousSquad(f"Several directory rows match {expected_title!r}")
        if matches:
            title = matches[0]
    if not in_memberships:
        raise Stop("The Squads directory did not expose the membership section")
    if not title:
        raise MissingSquad(f"Squad {expected_title!r} was absent from the complete directory scan")
    for page in pages(market.driver, -1):
        candidates = [node for label, node in page.rows if normalized(label) == normalized(title)]
        if len(candidates) > 1:
            raise AmbiguousSquad(f"Several visible rows match {title!r}")
        if candidates and market.driver.control_hittable(candidates[0]):
            market.driver.tap_control(candidates[0])
            return market.wait(lambda view: current_title(view, title),
                               f"the verified {title!r} chat")
    raise Stop(f"The matching squad {title!r} was found but could not be opened")


def read_state(target):
    if not target.state.exists():
        return {}
    state = json.loads(target.state.read_text())
    identity = state.get("identity", {})
    if (state.get("version") != 1 or identity.get("prefix") != target.prefix
            or normalized(identity.get("squad", "")) != normalized(target.squad)
            or identity.get("alphabet") != DEFAULT_ALPHABET):
        raise Stop(f"Checkpoint for job {target.key!r} belongs to a different run")
    return state


def summary(state):
    confirmed = set(state.get("confirmed", []))
    unavailable = set(state.get("unavailable", {}))
    return dict(recorded=len(confirmed), unavailable=len(unavailable), searched=state.get("searched", 0),
                queued=len(state.get("remaining", [])), pending=len(state.get("pending", [])),
                unresolved=len(state.get("unresolved", {})),
                unaccounted=len(set(state.get("candidates", {})) - confirmed - unavailable))


def finished(state):
    totals = summary(state)
    return bool(state and totals["searched"] and
                not any(totals[key] for key in ("queued", "pending", "unresolved", "unaccounted")))


def load_config(path):
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("udid"), str) or not data["udid"].strip():
        raise Stop("The queue configuration needs an iPhone UDID")
    if data.get("team_id") and not data.get("wda_bundle_id"):
        raise Stop("team_id requires wda_bundle_id")
    jobs = data.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise Stop("The queue configuration needs a nonempty jobs list")
    state_dir = (path.parent / data.get("state_dir", ".squad-inviter")).resolve()
    targets = {}
    for job in jobs:
        if not isinstance(job, dict):
            raise Stop("Each job must be a JSON object")
        key, prefix, squad = job.get("id"), job.get("prefix"), job.get("squad")
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", key) or key in targets:
            raise Stop("Each job needs a unique lowercase ID using letters, digits, underscores or hyphens")
        if not isinstance(prefix, str) or not USERNAME.fullmatch(prefix) or prefix.startswith("@"):
            raise Stop(f"Job {key!r} has an invalid username prefix")
        if not isinstance(squad, str) or not squad.strip():
            raise Stop(f"Job {key!r} needs an exact squad title")
        targets[key] = Target(key, prefix.casefold(), squad.strip(), state_dir / f"{key}-run.json")
    derived_data = data.get("wda_derived_data")
    return SimpleNamespace(
        path=path, state_dir=state_dir, targets=targets, udid=data["udid"],
        server=data.get("server", "http://127.0.0.1:4723"), team_id=data.get("team_id"),
        wda_bundle_id=data.get("wda_bundle_id"), wda_url=data.get("wda_url"),
        wda_derived_data=(path.parent / derived_data).resolve() if derived_data else None,
        invite_actor=data.get("invite_actor"),
    )


def configuration(config, target, separate_taps):
    return SimpleNamespace(
        server=config.server, udid=config.udid, bundle_id=BUNDLE,
        team_id=config.team_id, wda_bundle_id=config.wda_bundle_id, wda_url=config.wda_url,
        wda_derived_data=config.wda_derived_data, squad=None, prefix=target.prefix,
        alphabet=DEFAULT_ALPHABET, state=target.state,
        open_via=["cui.iconButton.invite", "cui.iconButton.find-friends"],
        verify_squad=True, search_delay=0.15, empty_observation_delay=0.75, empty_search_delay=1.15,
        invite_actor=config.invite_actor, incremental_search=True, scroll_fraction=1.0,
        fast_swipe_drag_ms=150, fast_swipe_hold_ms=60, fast_swipe_settle=0,
        independent_touch_events=not separate_taps,
    )


def finish_squad(market, journal):
    screen = market.open_picker()
    if not screen.exact(journal.identity["squad"]):
        raise Stop("The expected squad title is missing from Find Friends")
    if screen.keyboard:
        screen = market.dismiss_keyboard(screen, screen.field().attributes.get("value", ""))
    data = journal.data
    if len(screen.selected_accounts) != screen.selected:
        raise Stop("The picker selections could not be mapped to exact account IDs")
    if data["pending"]:
        if screen.selected:
            raise Stop("A pending submission still has staged selections")
        verify_batch(market, journal)
    batch = VisibleBatch(market, journal, BATCH_CAP)
    allowed = data["candidates"].keys() - set(data["confirmed"]) - data["unavailable"].keys()
    if not screen.selected_accounts <= allowed:
        raise Stop("The picker has selections outside this squad's checkpoint")
    for account in screen.selected_accounts:
        handle = data["candidates"][account]
        if not handle.casefold().startswith(journal.identity["prefix"]):
            raise Stop("A staged account does not match this squad's prefix")
        batch.entries[account] = {"account": account, "handle": handle,
                                  "query": data["origins"].get(account)}
    if batch.entries:
        batch.started_at = time.monotonic()
        print(f"Recovered {len(batch.entries)} checkpointed selections.", flush=True)
        if len(batch.entries) >= BATCH_CAP:
            batch.flush()
    discover(market, journal, batch, ordered=True)
    for query in tuple(data["unresolved"]):
        count, rows = batch.collect(query)
        if len(rows) == count:
            data["unresolved"].pop(query)
            journal.save()
    batch.flush()
    send(market, journal, batch_size=BATCH_CAP)
    if not finished(data):
        raise Stop("The squad still has unresolved checkpoint entries")


def worker(config, target, separate_taps):
    args = configuration(config, target, separate_taps)
    with target.state.with_suffix(".json.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        saved_title = read_state(target).get("identity", {}).get("squad")
        expected_title = saved_title or target.squad
        driver = Appium(args)
        try:
            active = driver.post("/execute/sync", {"script": "mobile: activeAppInfo", "args": []})
            if active.get("bundleId") != BUNDLE:
                driver.post("/execute/sync", {"script": "mobile: activateApp", "args": [{"bundleId": BUNDLE}]})
            market = Polymarket(driver, args)
            market.check_active_app()
            screen = close_settings(market, market.screen())
            title = current_title(screen, expected_title)
            if not title:
                go_to_directory(market, screen)
                screen = directory_target(market, expected_title)
                title = current_title(screen, expected_title)
            if not title or (saved_title and title != saved_title):
                raise Stop("Could not verify the intended squad chat title")
            args.squad = title
            journal = Journal(args)
            journal.save()
            print(f"Verified squad {title!r}. Running prefix {target.prefix!r}.", flush=True)
            finish_squad(market, journal)
            print(f"Finished {title!r}. {summary(journal.data)}", flush=True)
        finally:
            with contextlib.suppress(Stop):
                driver.close()


def refresh_helper(udid):
    command = ["xcrun", "devicectl", "device"]
    try:
        processes = subprocess.run(command + ["info", "processes", "--device", udid],
                                   capture_output=True, text=True, timeout=18)
        for line in processes.stdout.splitlines() if not processes.returncode else ():
            if line.strip().endswith("/WebDriverAgentRunner-Runner.app/WebDriverAgentRunner-Runner"):
                stopped = subprocess.run(command + ["process", "terminate", "--device", udid,
                                                    "--pid", line.split()[0]], capture_output=True, timeout=12)
                if not stopped.returncode:
                    print("Refreshed the iPhone automation helper.", flush=True)
    except (subprocess.TimeoutExpired, OSError):
        print("The phone did not respond to the helper check.", flush=True)


def run_queue(config):
    report = {"order": list(config.targets), "entries": {}}
    report_path = config.state_dir / "squad-queue-status.json"

    def record(target, status, reason=None):
        state = read_state(target)
        entry = {"status": status, "prefix": target.prefix,
                 "squad": state.get("identity", {}).get("squad", target.squad), **summary(state)}
        if reason:
            entry["reason"] = reason
        report["entries"][target.key] = entry
        temporary = report_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(report_path)
        print(f"QUEUE {target.prefix} {status}: {entry['recorded']} recorded"
              + (f" ({reason})" if reason else ""), flush=True)

    failed = False
    with (config.state_dir / "squad-queue.lock").open("a") as queue_lock:
        fcntl.flock(queue_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for target in config.targets.values():
            with (config.state_dir / f"{target.key}-supervisor.lock").open("a") as target_lock:
                try:
                    fcntl.flock(target_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    record(target, "waiting for active worker")
                    fcntl.flock(target_lock, fcntl.LOCK_EX)
                unchanged = 0
                separate_taps = False
                while not finished(read_state(target)):
                    record(target, "running")
                    before = summary(read_state(target))
                    log_path = config.state_dir / f"{target.key}-run.log"
                    command = [sys.executable, "-u", str(Path(__file__).resolve()),
                               str(config.path), "--worker", target.key]
                    if separate_taps:
                        command.append("--separate-taps")
                    with log_path.open("ab") as log:
                        offset = log.tell()
                        status = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                                pass_fds=(queue_lock.fileno(),)).returncode
                    with log_path.open("rb") as log:
                        log.seek(offset)
                        lines = log.read().decode(errors="replace").splitlines()
                    reason = next((line for line in reversed(lines) if line.strip()), f"Worker exited ({status})")
                    if status in (MISSING, AMBIGUOUS):
                        record(target, "missing" if status == MISSING else "ambiguous", reason)
                        failed = True
                        break
                    if status in (130, -2, -15):
                        record(target, "paused", reason)
                        return 130
                    after = summary(read_state(target))
                    if finished(read_state(target)):
                        break
                    unchanged = unchanged + 1 if after == before else 0
                    print(f"QUEUE {target.prefix} worker exited ({status}); retrying from checkpoint. {reason}", flush=True)
                    if "rapid taps changed an unexpected" in reason:
                        separate_taps = True
                    if "XCTDaemonErrorDomain Code=41" in reason or (unchanged == 1 and "socket hang up" in reason):
                        refresh_helper(config.udid)
                    needs_phone = any(word in reason.casefold() for word in
                                      ("unknown device or simulator", "device is locked", "unlock the device",
                                       "passcode", "device is not connected", "device could not be found"))
                    if unchanged >= 3 and not needs_phone:
                        record(target, "stopped", reason)
                        return 1
                    if needs_phone and unchanged == 3:
                        record(target, "waiting for the iPhone", reason)
                    time.sleep(60 if needs_phone and unchanged >= 3 else 5)
                if finished(read_state(target)):
                    record(target, "complete")
        print("QUEUE finished.", flush=True)
        return int(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="Path to the local queue JSON")
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    parser.add_argument("--separate-taps", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args()
    try:
        config = load_config(options.config.resolve())
        config.state_dir.mkdir(parents=True, exist_ok=True)
        if options.worker:
            worker(config, config.targets[options.worker], options.separate_taps)
            return 0
        return run_queue(config)
    except MissingSquad as error:
        print(error, flush=True)
        return MISSING
    except AmbiguousSquad as error:
        print(error, flush=True)
        return AMBIGUOUS
    except KeyboardInterrupt:
        print("Queue paused at its saved checkpoint.", flush=True)
        return 130
    except (Stop, OSError, ValueError, TypeError, KeyError, ET.ParseError) as error:
        print(f"Stopped: {error}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
