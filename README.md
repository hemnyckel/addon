# Hemnyckel

**A first-class, private companion for smart locks — built the way Apple would build it, only for one family and one home.**

Hemnyckel is a small system with one job: tell the family *who, when and how* a
door was opened, and let them lock or unlock it — natively, instantly, and
without ever opening a dashboard.

- **iOS app** (SwiftUI) — rich push notifications, Lock/Unlock actions, history,
  widgets, Live Activities, Control Center, Siri, Apple Watch.
- **Local push relay** (Home Assistant add-on) — subscribes to your local lock
  events and sends Apple Push Notifications. Runs in your home; no vendor cloud.
- **Home Assistant integration** — the source of truth. It decodes the lock's
  operation events (slot + method: keypad / fingerprint / tag / auto) and
  journals them.
- **Apple Home / Google Home** — parallel native control surface via Matter.

It supports **N locks** (three or more from the start), and each lock, person and
notification preference is configurable.

> **Unofficial and unaffiliated.** This is an independent project. It is **not
> affiliated with, endorsed by, or supported by** any lock vendor. Product names
> and trademarks belong to their respective owners and are used only to describe
> compatibility. It controls physical doors — use it entirely at your own risk.

See `docs/` for the design, the notification contract and the relay API.
