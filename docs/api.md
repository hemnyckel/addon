# Relay API — Hemnyckel

The relay runs as a Home Assistant add-on inside the home. The iOS app talks to
it over HTTPS (on the LAN, or through a VPN / Home Assistant Cloud when away).
There are **no accounts** — pairing is a one-time code, and every device gets its
own bearer token.

Base URL: `https://<relay-host>/api`

## Pairing

The relay shows a short-lived pairing code (in the add-on log / a Home Assistant
notification). The app scans a QR code or types the code.

```
POST /pair            { "code": "123456", "name": "Claes' iPhone" }
  -> 200 { "device_token": "<opaque>", "relay_id": "<uuid>" }
  -> 401 invalid or expired code
```

Codes are single-use and expire after a few minutes.

## Registration & preferences

```
POST /register        Authorization: Bearer <device_token>
  { "apns_token": "<hex>", "person": "claes", "apns_env": "development", "prefs": {...} }
  -> 200 { "ok": true }
```

`apns_token` is the Apple device token. `person` links the device to a family
member so notifications can be targeted. `apns_env` is `development` (Xcode debug
builds, APNs sandbox) or `production` (TestFlight / App Store); the relay stores
it per device and sends to the matching APNs host. `prefs` is a small object (see
below).

`apns_token` may be **empty** — a device without push (a simulator, or before an
APNs key exists) should still register, so the relay learns its `person` and can
attribute an app-initiated lock/unlock to them.

## Events

```
GET /events?since=<epoch>&door=<id>&person=<id>&limit=<n>
  -> 200 { "events": [Event, ...] }      # newest last
GET /ws                                  # live Event stream (WebSocket)
```

Apps and widgets read history from here. The relay also keeps a bounded local
cache, so the app still shows the last events when Home Assistant is briefly
unavailable.

## State

```
GET /state
  -> 200 {
      "doors": [ { "id":"front","name":"...","locked":true,"open":false,
                   "battery":92,"last_event":Event } ],
      "presence": { "claes":"home", "anna":"away" },
      "relay": { "online": true, "apns": true }
    }
```

## Actions

```
POST /action          Authorization: Bearer <device_token>
  { "door": "front", "action": "lock" | "unlock" }
  -> 200 { "ok": true, "confirmed": true }
  -> 409 { "ok": false, "detail": "the lock did not confirm" }
```

Actions are forwarded to Home Assistant. If the lock does not confirm, the app
shows a clear, recoverable state — never a silent success.

When the lock reports the operation back, the relay **credits it to the person**
whose device asked for it: the event becomes `source: "app"`, `method: "App"` and
that person, so history reads "Claes · App" instead of "Oattribuerad" (and the
"never notify the person who acted" rule works for app actions too). The credit
is short-lived and only ever applies to a report that carries no attribution of
its own — a keypad or fingerprint entry is never overwritten.

## Live Activities

While a door is unlocked, the relay keeps a Live Activity (Lock Screen +
Dynamic Island) in step with it. All three calls are authenticated with the
device token:

```
POST /live/start-token   { "apns_token": "<hex>" }
  -> 200 { "ok": true }        # the ActivityKit push-to-start token

POST /live/activity      { "door": "front", "apns_token": "<hex>" }
  -> 200 { "ok": true }        # a running activity's per-activity token
  -> 400 unknown door

DELETE /live/activity?door=front
  -> 200 { "ok": true }        # the app ended the activity
```

The relay owns the start/update/end pushes itself (topic
`<bundle>.push-type.liveactivity`), driven by the same journal events that
produce notifications: an unlock starts or updates, a lock (including
auto-relock) ends.

## People (owner only)

The first device to pair owns the install; every later device is a user. The
relay enforces this, so a user never receives what it may not see.

```
GET /devices                     # owner only; 403 otherwise
  -> 200 { "devices": [ { "id": "...", "name": "Claes' iPhone", "person": "Claes",
                          "role": "owner", "created": 1758... } ] }

POST /devices/<id>/role          # owner only
  { "role": "owner" | "user" }
  -> 200 { "ok": true }
  -> 409 the last owner cannot be demoted

POST /pair-code                  # owner only; a fresh code for a new device
  -> 200 { "code": "A1B2C3", "expires_in": 600 }
```

`GET /state` also carries the caller's `role`, `device_id` and — for a guest —
`expires`, so the app can show only what the role may.

### Guests

An owner invites a guest with a **name, the doors they may use and a window**.
The guest redeems the code with `POST /pair` and becomes a `guest` device.

```
POST /invites                    # owner only
  { "name": "Städning", "doors": ["front"], "expires_in_minutes": 120 }
  -> 200 { "code": "A1B2C3", "doors": ["front"], "expires_at": 1758... }

DELETE /devices/<id>             # owner only
  -> 200 { "ok": true }
  -> 409 you cannot remove your own device, or the last owner
```

A guest device:

- sees **no history** (`GET /events` → `[]`) and **no presence**;
- sees and acts on **only their doors** (`POST /action` → 403 otherwise);
- is **never notified** and registers no push tokens;
- is refused entirely once `expires` has passed (403).

## Health

```
GET /health      -> { "status": "ok", "ha": true, "apns": true, "version": "..." }
GET /api/health  -> the same
```

The root path stays open for probes; `apns` is true only when a real key is
loaded.

## Event object

See `shared/events.schema.json`. In short:

```
{ "id", "ts", "door", "person", "slot", "action": "lock"|"unlock",
  "source": "keypad"|"finger"|"tag"|"auto"|"unattributed",
  "method": "free text", "door_open": true|false }
```

## Security

- Pairing codes are single-use and time-limited.
- Per-device bearer tokens; revocable from the add-on.
- HTTPS only (LAN, VPN or Home Assistant Cloud). No vendor cloud.
- The relay stores APNs tokens and preferences locally, and nothing else.
