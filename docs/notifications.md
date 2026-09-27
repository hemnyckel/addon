# Notifications — Hemnyckel

Notifications are the product. They must be worth interrupting someone for, and
never become noise.

## Content

A notification answers *who, when, how, which door*:

- **Title** — the door ("Ytterdörren").
- **Subtitle** — person · method ("Elise · Fingeravtryck").
- **Body** — action + local time ("Låstes upp 16:12").
- **Grouping** — `threadIdentifier` per door, so a busy door collapses neatly.
- **Enrichment** — `mutable-content: 1`; a Notification Service Extension fills in
  detail (and a door-open context when a Thread sensor exists).

## Categories and actions

Category `DOOR_EVENT`:

- **Lås upp** — `.authenticationRequired` → Face ID, then `POST /action`.
- **Lås** — immediate.
- **Visa** — opens the door's History.
- **Tysta 1 h** — per-device, per-door snooze.

Action tint follows HIG; nothing destructive hides behind a tap.

## Interruption levels

- Arrivals/departures for children → `time-sensitive`.
- Routine events → normal.
- **Critical** only if a genuinely critical case exists (requires an Apple
  entitlement) — never used casually.

## Targeting and preferences

Per person and per device:

- Which events notify you (all / family only / children only / none).
- Which doors.
- Quiet hours (and Focus integration: a "Family" Focus may still let child
  events through).
- Coalescing: "3 händelser sen senast" instead of a burst.

## Live Activities

While a door is unlocked, a Live Activity shows the state on the Lock Screen and
in the Dynamic Island (compact = state, expanded = last person + the action).
The card carries a **Lås** button while the door is unlocked. When the door
locks, the card flips to **Låst** and lingers about a minute — an undo window in
which the button reads **Lås upp** — then ends and dismisses itself.

The buttons are App Intents (`LiveActivityIntent`) that run in the app's process,
call the relay, and only reflect the new state once the lock *confirms* — a
failed action never looks successful.

## Anti-noise rules

1. Never notify the person who performed the action (unless configured).
2. Batch routine activity.
3. Respect quiet hours and Focus.
4. A failed action never reports success; a stale state never looks live.
