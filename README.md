# Hemnyckel — Home Assistant add-ons

This repository is a **Home Assistant add-on repository**. It currently ships one
add-on:

| Add-on | What it does |
|---|---|
| **Hemnyckel** (`relay/`) | Turns local lock events into Apple push notifications, and lets the Hemnyckel app lock and unlock your doors. |

## Install

1. Home Assistant → **Settings → Add-ons → Add-on Store**.
2. **⋮ → Repositories** → add `https://github.com/hemnyckel/addon`.
3. Install **Hemnyckel**, set your doors and your APNs key, start it.

No Home Assistant credentials are needed: the add-on gets them from the supervisor.

## The app is the point

This add-on exists to feed **Hemnyckel**, the family's own iOS app: rich notifications with
*who opened the door, when and how*, history, guests and lock control — with no Home Assistant
app and no vendor app. The app is not in the App Store yet; it is heading for TestFlight, and
the add-on collects and stores everything meanwhile, so nothing is lost while it is on its way.

| Part | Repository | |
|---|---|---|
| The iOS app | `hemnyckel/ios` | private; TestFlight next |
| The lock integration (local: ZHA, slots, journal) | [`hemnyckel/integration`](https://github.com/hemnyckel/integration) | HACS |
| The relay (this add-on) | this repository | the add-on store |

The add-on's own documentation — options, doors, APNs setup, health — is in
[`relay/README.md`](relay/README.md).

## Where the rest of Hemnyckel lives

## License

Apache License 2.0 — see [LICENSE](LICENSE).
