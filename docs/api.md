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
  { "apns_token": "<hex>", "person": "claes", "prefs": {...} }
  -> 200 { "ok": true }
```

`apns_token` is the Apple device token. `person` links the device to a family
member so notifications can be targeted. `prefs` is a small object (see below).

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

## Health

```
GET /health -> { "status": "ok", "ha": true, "apns": false, "version": "..." }
```

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
