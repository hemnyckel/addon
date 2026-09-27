# Packaging — how a family installs and keeps Hemnyckel

The goal: **one place to install, nothing to type.** A family member should never
see a URL, a token or a terminal.

## What the solution is made of

| Part | Runs where | Distributed as |
|---|---|---|
| Home Assistant OS | the home box | Home Assistant's own installer |
| The lock integration (local: ZHA, slots, journal) | Home Assistant core | **HACS**, custom repository |
| The Hemnyckel relay | a Home Assistant **add-on** | **this repository** as an add-on repository |
| The Hemnyckel app | the family's iPhone/iPad | **TestFlight** first, then the **App Store** |
| Matter bridge (later) | a Home Assistant add-on | this repository, or a community add-on |

Two rules keep it simple:

1. **Nothing vendor-owned.** No Nimly cloud, no bridge, no third-party app. The
   only external hop is Apple Push (APNs), outbound from the home.
2. **The home is the source of truth.** Home Assistant already knows the locks
   (ZHA), the people (slots) and the history (the journal). The relay and the app
   are thin fronts for it.

## The relay is an add-on repository

`repository.yaml` at the root makes this repository installable in **Settings →
Add-ons → Add-on Store → ⋮ → Repositories**. The add-on itself lives in `relay/`
(its `slug` is `hemnyckel`, which is what matters, not the folder name):

```
addon/                     this repository (github.com/hemnyckel/addon)
├── repository.yaml        ← makes it an add-on repository
├── relay/                 ← the add-on
│   ├── config.yaml        name, version, options, schema
│   ├── Dockerfile         built on the Home Assistant host
│   ├── app/               the relay itself
│   └── README.md          what users read in the store
├── docs/                  design, notifications, api, apns, packaging
├── tools/                 development helpers
└── shared/                the event schema both sides agree on
```

Hemnyckel is three repositories, so each part has one home:

| Part | Repository | Visibility |
|---|---|---|
| This add-on (the relay) | `hemnyckel/addon` | public |
| The lock integration | `hemnyckel/integration` | public (HACS requires it) |
| The iOS app | `hemnyckel/ios` | private |

**No Home Assistant credentials are configured.** The add-on declares
`homeassistant_api: true`, and the supervisor injects `SUPERVISOR_TOKEN`, which
the relay picks up automatically (`http://supervisor/core`). `ha_url` / `ha_token`
remain only for standalone development.

**Updating** is one version bump in `relay/config.yaml` — Home Assistant then
offers the update to every installation.

## The integration is a HACS repository

The lock integration lives at **`hemnyckel/integration`** — Hemnyckel's own
integration, the `nimly` project renamed, with the domain `hemnyckel` with `hacs.json`, semantic git tags and release notes. The
family installs it through **HACS → custom repository**, and updates arrive like
any other HACS integration.

The integration keeps **only the local half**: ZHA control, slot and credential
management (PIN, RFID, fingerprint, enrollment, slot names), the guest-code
services and the **journal** the relay reads. The **vendor cloud is gone**
(v2.0.0). The **bridge and the emulator** — the parts that keep the vendor app
in sync — are retired and are removed in the next release (v2.1), as a separate
deliberate change: they sit inside the mirror engine itself (MQTT, the channel
system, the emulator entities), so they are a rework of the local engine rather
than a deletion. The full project stays recoverable from the tags (`v1.0.14` is
the last complete state).

The relay depends on two things from the integration, so they are part of its
version requirement: the `hemnyckel_door_event` event, and a stable door key
on it. That key is the **lock entity** (`lock.ytterdorren`): the config entry
id is re-minted whenever the integration re-creates its entry, so it is only
a hint, while the lock entity is the same door for good.

## The app goes in the App Store

Source in `hemnyckel/ios` (**private**), generated with XcodeGen (no `.xcodeproj`
in git). Versioned with `MARKETING_VERSION` / `CURRENT_PROJECT_VERSION`. TestFlight
for the family first, then the normal App Store flow. No account, no login:
pairing is a **QR code** from an owner's device.

## What a family does, once (~15 minutes)

1. Home Assistant OS running, the locks paired in **ZHA**.
2. **HACS** → add the lock-integration repository → install → the mirrors appear.
3. **Add-on Store** → add this repository → install **Hemnyckel** → fill in the
   doors and the APNs key (`.p8`, Key ID, Team ID).
4. **App Store** → install Hemnyckel on each phone → the owner mints an
   invitation → everyone else **scans a QR code**.
5. Optional, later: Apple Home / Google Home through the Matter bridge add-on.

## Secrets and data

- The APNs `.p8` lives in the add-on's `/data` (private, persistent) or inline.
- The per-device relay token lives in the **iOS Keychain**; pairing codes are
  single use and short-lived; a revoked device forgets itself and can re-pair.
- The relay's database (`/data/hemnyckel.db`) holds paired devices and the event
  cache — and is included in Home Assistant snapshots automatically, because
  add-on data is.

## Operations

- **Health:** `GET /health` (or `/api/health`) — Home Assistant reachable, APNs
  configured, doors configured.
- **Logs:** the add-on log is the source of truth; pairing codes and guest
  invitations are printed there for the owner.
- **Updates:** Home Assistant updates the add-on, HACS updates the integration,
  the App Store updates the app — three independent, safe channels.

## Roadmap

- **Matter bridge** — the locks in Apple Home and Google Home (control stays
  native there; the app keeps the who/when/how).
- **Notification extension** — richer notifications and on-device translation.
- **Apple Watch** — lock and unlock from the wrist.
