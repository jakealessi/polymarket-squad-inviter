# Polymarket Squad Inviter

Python tools for finding Polymarket usernames by prefix and inviting eligible accounts to a named Squad. They run on a Mac and drive the Polymarket iPhone app through Appium and WebDriverAgent. Both programs use the Python standard library.

| Program | What it does |
| --- | --- |
| [squad_inviter.py](squad_inviter.py) | Discovers candidates for one prefix and optionally sends invitations. |
| [run_squad_queue.py](run_squad_queue.py) | Opens configured Squads, runs jobs in order, and retries interruptions from saved checkpoints. |

## Setup

Use macOS with Xcode, Python 3.10 or later, and a Node.js version supported by Appium. Connect a trusted iPhone over USB, unlock it, and enable Developer Mode and UI Automation. Configure WebDriverAgent signing with your Apple Development team and find the iPhone’s hardware UDID in Xcode. [Apple Developer Mode guide](https://developer.apple.com/documentation/xcode/enabling-developer-mode-on-a-device) · [Appium device setup](https://appium.github.io/appium-xcuitest-driver/latest/getting-started/device-setup/)

Install Appium and the XCUITest driver, then leave the server running. [Appium installation guide](https://appium.io/docs/en/latest/quickstart/install/)

```sh
npm install -g appium
appium driver install xcuitest
appium --address 127.0.0.1
```

## Run one prefix

Open the intended Squad chat in Polymarket. The example searches for usernames beginning with `alex` and saves its progress in a local JSON checkpoint. Add `--send` to submit invitations.

```sh
python3 squad_inviter.py alex --squad "Example Squad" \
  --udid "IPHONE_HARDWARE_UDID" --team-id "APPLE_DEVELOPER_TEAM_ID" \
  --wda-bundle-id "YOUR_WDA_BUNDLE_ID" \
  --open-via cui.iconButton.invite --open-via cui.iconButton.find-friends
```

Use `--from-picker` if Find Friends is already open in the intended Squad and no accounts are selected. Repeat `--open-via` for each control in a different navigation path. Use `--wda-url` to connect to an existing WebDriverAgent instance. Add `--invite-actor YOUR_USERNAME` to check the chat for invite receipts.

Rerun with the same Squad, prefix, and checkpoint to resume. Use `--state PATH` for a custom checkpoint and `--batch-size` for batches smaller than 80.

## Run a queue

Copy [queue.example.json](queue.example.json) to `queue.local.json` and fill in your device details. In `jobs`, give each job a stable lowercase `id`, a username `prefix`, and the full `squad` title shown in Polymarket. The same Squad can appear in multiple jobs.

```sh
cp queue.example.json queue.local.json
python3 run_squad_queue.py queue.local.json
```

The queue sends invitations after verifying the squad, records progress in `.squad-inviter/`, and skips completed jobs when rerun with the same configuration. It reports missing or ambiguous squads and continues to later jobs. Optional configuration fields include `server`, `wda_url`, `wda_derived_data`, and `invite_actor`. Relative paths resolve from the JSON file’s directory. Keep the local configuration, checkpoints, and logs on your machine.

## Search and verification

- Polymarket displays up to 20 search results. Capped searches expand into narrower prefixes, and an empty single-character search expands into two-character searches. Digits come first, followed by dot, underscore, letters, and hyphen. Use `--no-ordered-results` with the single-prefix tool to probe every child branch when a capped page is split.
- The inviter uses account IDs to track selections and reads account status to skip existing members and invitations. Coverage depends on Polymarket’s search index, supported username characters, and accessible result rows. Review `unresolved` in the checkpoint for searches with incomplete coverage.
- One matching chat receipt or confirmed account marks the whole submitted batch complete. The remaining accounts are inferred from that confirmation. A batch stays pending when the program cannot verify any of its accounts.

## Local tests

```sh
python3 tests/run_tests.py
```

The regression checks use synthetic accessibility fixtures and simulated Appium interactions. They cover search traversal, selection, batch limits, checkpoints, and queue recovery.
