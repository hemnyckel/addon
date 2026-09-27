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

The add-on's own documentation — options, doors, APNs setup, health — is in
[`relay/README.md`](relay/README.md).

## Where the rest of Hemnyckel lives

| Part | Repository |
|---|---|
| The lock integration (local: ZHA, slots, journal) | [`hemnyckel/integration`](https://github.com/hemnyckel/integration) |
| The iOS app | private |
| This add-on (the relay) | this repository |

## License

Apache License 2.0 — see [LICENSE](LICENSE).
