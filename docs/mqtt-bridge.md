# The MQTT bridge: the family's people in Home Assistant

The app is where the family manages who is who. Home Assistant owns the *locks*. This bridge
lets Home Assistant **see** the people and **change their role** — between `owner` and `user`,
the two permanent roles — without moving the truth anywhere: the relay's database stays
authoritative and the relay keeps enforcing. A guest is deliberately not one of the roles the
bridge can set; see [Why guests are not a bridge role](#why-guests-are-not-a-bridge-role).

## Why MQTT and not something else

- An add-on **cannot register Home Assistant services** — only an integration can. A custom
  integration for the relay would be a fourth component whose only job is to be a thin client.
- Home Assistant already speaks MQTT, and MQTT **discovery** turns a published JSON document
  into a real entity with a real control (`select`) — no integration required.
- Home Assistant already knows the broker: an add-on that asks for it (`services: mqtt:want`)
  is handed the broker's host and credentials by the Supervisor, so nobody types a broker
  address or a password. *How* it is handed over depends on the Supervisor version — see
  [How the relay finds the broker](#how-the-relay-finds-the-broker).

## The one rule this design keeps

> Role enforcement lives in the relay. The bridge is a *projection* and a *command channel* —
> never a second place where permission is decided.

So: the relay publishes what is true, Home Assistant sends what it wants, the relay decides,
and then the relay publishes the new truth. If a command is refused, the state does not change
and the Home Assistant control snaps back on its own. That is the feedback mechanism — there is
no separate result topic to keep in sync.

## Topics

Namespace `hemnyckel/…` for our own topics; Home Assistant's discovery prefix stays the
default (`homeassistant/…`). Everything the relay publishes is **retained**, so Home Assistant
sees the current truth immediately after a restart instead of waiting for the next change.

| Direction | Topic | Payload |
|---|---|---|
| relay → HA | `hemnyckel/relay/state` | `{"status":"ok","ha":true,"apns":false,"doors":2,"version":"0.8.4","last_event_at":1758…,"journal_read_at":1758…,"journal_ok":true,"events":232}` — the same facts as `/health`, retained |
| relay → HA | `hemnyckel/relay/availability` | `online` / `offline` (also the client's last will, so a dead relay says so) |
| relay → HA | `hemnyckel/people/<slug>/state` | the person, see below |
| HA → relay | `hemnyckel/people/<slug>/role/set` | `owner` or `user` — nothing else |

The relay's own state document is republished when the Home Assistant connection comes up —
the broker connects first, so the initial document says `"ha": false` — and again on a one-minute
timer. That is what keeps the retained document equal to `/health`: a slow change (APNs coming
up, a door added, a new version) is corrected within a minute instead of staying stale forever.

The document carries the journal's pulse in two independent halves: `last_event_at` is the
**ingest** truth (the newest event stored, `null` on an empty journal) and `events` is how much
the relay holds, while `journal_read_at` is the newest event the **read path** actually hands
back and `journal_ok` says whether the read side is keeping up with ingest. They are separate
because the first journal bug was invisible to an ingest-only pulse — the events arrived and
were stored, but the read path returned the oldest window, so the app's History froze. Home
Assistant watches both, so a read-side fault raises its own alarm instead of masquerading as
"the journal is quiet".

`<slug>` is the person's name lowercased with `[^a-z0-9]+` folded to `-` (stable, readable, and
what the discovery topic uses too).

A person's state document:

```json
{
  "person": "Elise Högberg",
  "id": "8f2c9a1b3d4e5f60718293a4b5c6d7e8",
  "role": "user",
  "avatar_kind": "symbol",
  "avatar_symbol": "star",
  "avatar_color": "#FF9500",
  "avatar_version": 3,
  "active": true,
  "devices": [
    {"id": "0a1b…", "name": "iPhone 13", "model": "iPhone 13", "os": "18.7",
     "role": "user", "last_seen": 1790618400}
  ],
  "doors": ["front"],
  "window": {"days": [1, 3], "from": "08:00", "to": "17:00"},
  "expires": 1791000000
}
```

`id` is the person's stable identity, and the `avatar_*` fields are their icon —
additive fields Home Assistant reads to give the person's entity an icon and an
`entity_picture`. `avatar_kind` is `monogram`, `symbol` or `photo`;
`avatar_symbol`/`avatar_color` are present only for a symbol (the shared
twenty-token vocabulary and a `#RRGGBB`); `avatar_version` changes on every icon
change, so a stale picture is detectable. The photo bytes never cross the bridge;
only the descriptor does. `doors`, `window` and `expires` only mean something for a guest; they are omitted otherwise.
A guest also carries `guest_configured`: `true` when the guest really has a life (doors, hours
or an end date), and `false` when the role is `guest` but none of them is set. A false value
means the relay is reading that person as *all doors, any time, for ever* — a role set from
Home Assistant before guests stopped being a bridge role — and it is visible so the owner can
give the guest a life in the app. An edit in the app clears it.

## Entities in Home Assistant

Discovered under **one device** — the relay itself — so the family's device list stays honest:
the locks are the integration's devices, the relay is this one.

| Entity | Kind | What it is |
|---|---|---|
| `select.hemnyckel_<slug>` | `select`, options `owner`/`user` | the person's role. Its attributes carry the devices, the guest window and the expiry, so one row tells the whole story |
| `sensor.hemnyckel_relaet` | `sensor` | the relay: state `ok`, attributes `ha`, `apns`, `doors`, `version`, and the journal's pulse — `last_event_at`, `journal_read_at`, `journal_ok`, `events` |
| `binary_sensor.hemnyckel_apns` | `binary_sensor`, device class `connectivity` | whether push is configured — the thing you want to glance at the day the Apple key lands |

Discovery payloads (relay → `homeassistant/<component>/hemnyckel/<object>/config`, retained):

```json
{"name": "Elise Högberg", "unique_id": "hemnyckel_person_elise-hogberg",
 "state_topic": "hemnyckel/people/elise-hogberg/state",
 "command_topic": "hemnyckel/people/elise-hogberg/role/set",
 "value_template": "{{ value_json.role }}", "options": ["owner", "user"],
 "json_attributes_topic": "hemnyckel/people/elise-hogberg/state",
 "availability_topic": "hemnyckel/relay/availability",
 "icon": "mdi:star",
 "entity_picture": "<ha-origin>/api/hemnyckel/avatar/<id>?v=<avatar_version>",
 "device": {"identifiers": ["hemnyckel_relay"], "name": "Hemnyckel",
            "manufacturer": "Hemnyckel", "model": "Reläet",
            "configuration_url": "https://ljungen.hall-hogberg.se/hemnyckel/"}}
```

`unique_id` is the person's slug, so a rename in the app updates the friendly name instead of
creating a second entity — and an entity the family has customised keeps its customisation.

The `icon` is the avatar's: a symbol's Material Design glyph (the shared token
vocabulary), else the generic key. `entity_picture` is set **only for a photo** —
a monogram or a symbol is drawn by the client from the state attributes — and it
points at the Home Assistant integration's authenticated view
(`/api/hemnyckel/avatar/<id>`), with `?v=<avatar_version>` so a changed photo is a
changed URL. The relay mirrors each photo to `/share/hemnyckel/avatars/<id>.jpg`
(the add-on maps `share:rw`) and the integration serves it; the bytes never travel
over MQTT and never sit on an unauthenticated path.

The view path is prefixed with **Home Assistant's own origin** (`<ha-origin>`),
taken from `GET /api/config` (`internal_url`, else `external_url`) when Home
Assistant connects. This is not decoration: Home Assistant validates an MQTT
`entity_picture` with `cv.url` and **rejects a relative path**, so a photo is
published only once that origin is known — with none, the field is left off and
the tile falls back to the avatar's `icon` rather than carrying a URL Home
Assistant would refuse.

## The rules the relay applies to a command

1. The payload must be exactly `owner` or `user`; anything else is ignored and logged. A
   `guest` is refused with a logged reason and the truth is republished — see below.
2. An unknown slug is ignored and logged — never a new person.
3. **The last owner cannot be demoted.** If the command would leave the install without an
   owner, it is refused; this is the same rule the app's management endpoints already enforce.
4. Every accepted change is written through the *same* path the app uses (the same store call),
   so a role set from Home Assistant is indistinguishable from one set in the app.
5. After any change — accepted or refused — the relay republishes the person's state, so the
   Home Assistant control always shows the truth.
6. A person's state is republished when their devices change (a new pairing, a revocation, a
   rename, a new `last_seen`).

## Why guests are not a bridge role

A role alone carries no *life*. The relay's rules read an empty door list as **all doors**, an
empty window as **any time**, and no expiry as **never expires** — so a person whose role is set
to `guest` from Home Assistant is not a guest: it is a user without notifications, with every
door, for ever. That is the opposite of what "guest" means.

A guest is shaped, not labelled: **doors, weekdays, a window and an end date**, and each of them
writes a matching code on the chosen locks. That shaping — and editing it afterwards — is the
app's job (`Personer → a guest → Redigera`, backed by the relay's owner-only
`POST /people/{person}/guest`). The bridge therefore offers only the two permanent roles and
refuses `guest` outright.

Someone who became a guest from Home Assistant *before* this rule keeps that role — the bridge
never silently rewrites it. Their state carries `guest_configured: false`, so the unset life is
visible in Home Assistant and can be given a real life with one edit in the app.

## How the relay finds the broker

`services: mqtt:want` is still what makes Home Assistant responsible for the broker, but
*how* the Supervisor hands over the details depends on its version. The relay tries these
sources in order and uses the first one that has a complete host, username and password:

1. **The `MQTT_*` environment** (`MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME`,
   `MQTT_PASSWORD`, `MQTT_SSL`). This is the historic Supervisor injection, and it stays
   preferred so an installation that already works is untouched.
2. **The Supervisor's registered MQTT service** (`GET http://supervisor/services/mqtt`,
   authenticated with the add-on's own `SUPERVISOR_TOKEN`). Current Supervisor versions no
   longer inject `MQTT_*` into the container — the relay logs "Home Assistant did not
   provide broker credentials" on such a host even though Mosquitto is running and the
   add-on asks for MQTT. The Mosquitto app still *registers* the broker as service data,
   and an app whose `services` list includes `mqtt` may read it. This is the normal path
   today, and it stays credential-free from the family's point of view: the Supervisor
   hands over the broker's own `addons` account, not something anyone types.
3. **The add-on options** `mqtt_host`, `mqtt_port`, `mqtt_user`, `mqtt_password`. The
   honest last resort for a Supervisor that provides nothing at all: an owner can fill them
   in on the add-on page. `mqtt_password` is a `password` field, so it is not shown.

A source with only part of the three values is treated as absent, so the bridge never
connects without authentication.

The order is deliberate. The environment comes first for backwards compatibility; the
Supervisor's own registered service is preferred over anything typed on the add-on page;
and the options are the fallback that guarantees the bridge can always be made to run even
on a host whose Supervisor hands an app nothing.

## Security

- **The broker must not allow anonymous publishing.** The command topic decides who may
  manage the family's people, so it travels on an authenticated connection or not at all.
  The relay takes the broker's credentials from Home Assistant itself — the injected
  `MQTT_*` environment, or the Supervisor's registered MQTT service — and only if neither
  exists does it use the explicit `mqtt_*` options. If no source has them it logs once and
  stays off rather than falling back to anonymous.
- **What never crosses the bridge:** lock codes, device tokens, the APNs key, invite codes,
  and the avatar **photo bytes**. Only names, roles, the person's opaque id and icon
  descriptor (kind, symbol, colour, version), device metadata (name, model, OS, last seen)
  and the relay's own health.
- The Home Assistant side is guarded by Home Assistant's own authentication: changing a role
  from there needs an account that may use the MQTT integration.

## Failure behaviour

| Situation | What happens |
|---|---|
| Broker unreachable at start | The relay runs exactly as before; it logs once and retries in the background. The bridge is a convenience, never a dependency. |
| Broker drops mid-run | Availability goes `offline` (the last will), the entities grey out, and the relay reconnects. |
| A command arrives while the store is busy | Serialised with the rest of the relay's writes; the reply is the republished truth. |
| MQTT disabled in Home Assistant, and no `mqtt_*` options | no source has broker credentials; the bridge stays off, everything else is untouched. |

## What this deliberately is not

- **Not a second management UI.** The app remains the place for invitations, guests' doors and
  history; the bridge carries roles and health, and nothing else, so there is one place where
  each decision is made and one place where it is enforced.
- **Not a way around roles.** A `user` in Home Assistant cannot promote anyone: the command
  path is the relay's, and the relay decides.
- **Not a general relay API in Home Assistant.** If something else is ever wanted there, it gets
  its own deliberate topic and its own row in this document.
