# APNs — Apple push for Hemnyckel

The push chain: a lock event reaches Home Assistant, the integration journals it
(who / when / how), the relay maps it to a Hemnyckel event and sends one APNs
push to each paired iPhone. APNs is the only external hop; there is no vendor
cloud.

```
lock → ZHA → nimly journal → relay → APNs (.p8 token) → iPhone
```

## 1. Apple Developer Program

Enroll at [developer.apple.com](https://developer.apple.com/programs/) (the
account that will own the app). Note your **Team ID** (10 characters) under
*Membership*.

## 2. Create an APNs key

1. Developer portal → **Keys** → **+**.
2. Name it (e.g. `Hemnyckel APNs`) and tick **Apple Push Notifications service
   (APNs)**.
3. **Download the `.p8` file — Apple shows it exactly once.** Note the **Key ID**
   (10 characters).

One key works for **both** the sandbox and production environments and for every
app in the team, so you rarely need more than one. An account may hold two APNs
keys; revoke and rotate if one leaks.

> The `.p8` is a private key. `.gitignore` ignores `*.p8` — never commit it.

## 3. Give the key to the relay

The relay accepts either the **path** to the key or its **inline contents**.

**Home Assistant add-on (recommended).** Copy the file into the add-on's private,
persistent `/data` directory and point at it:

```yaml
apns_key: /data/AuthKey_ABC123DEFG.p8
apns_key_id: ABC123DEFG
apns_team_id: YOURTEAMID
```

**Standalone / development.** Either set `apns_key` to the path, or to the PEM
contents for a quick local run.

If the key cannot be read, the relay logs an error and runs in **dev mode**
(pushes are logged, not sent) instead of failing to start.

## 4. Sandbox vs production

`apns_env` chooses Apple's host:

| Value | Host | Used by |
|---|---|---|
| `development` (or `sandbox`) | `api.sandbox.push.apple.com` | Xcode debug builds |
| `production` | `api.push.apple.com` | TestFlight and App Store builds |

The iOS app sends its environment on `POST /register` (derived from `#if DEBUG`),
the relay stores it **per device**, and picks the matching host. `apns_env` in the
add-on options is only the fallback for devices that report none.

A **mismatch** shows up as `BadDeviceToken` / `DeviceTokenNotForTopic` from APNs.
The relay reacts by dropping that device's push token, and the app registers a
fresh one on its next launch — so a wrong environment self-heals.

## 5. What the relay sends

- **Provider token:** ES256 JWT (`kid` = Key ID, `iss` = Team ID), minted once and
  refreshed every **45 minutes**, and again immediately on `ExpiredProviderToken`.
- **Headers:** `apns-topic` = bundle id, `apns-push-type: alert`, `apns-priority:
  10`, `apns-expiration` (1 h), a per door+action `apns-collapse-id` (bursts
  collapse), and a unique `apns-id` per push.
- **Result handling:** `Unregistered` / `BadDeviceToken` / `DeviceTokenNotForTopic`
  → drop the token (the pairing survives); `429` / `500` / `503` and transport
  errors → retry with backoff (honouring `Retry-After`).

## 6. Verify

- `GET /health` reports `"apns": true` once a real key is loaded.
- Pair a phone, use a lock, and expect exactly one notification.
- Any APNs rejection is logged with its `reason` in the add-on log.

## Rotating a key

Create a new key, update `apns_key` / `apns_key_id`, restart the add-on, then
revoke the old key in the portal. The relay mints a new JWT on the next send.
