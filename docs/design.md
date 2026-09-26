# Design — Hemnyckel

A design brief for a native iOS app that should feel like part of the system:
clarity, deference, depth. No custom chrome, no decoration for its own sake.
**Glanceable first:** the common tasks (unlock, see who arrived) happen outside
the app — in a widget, a notification, Control Center or Siri. The app is the
deep end.

## Principles

1. **Part of iOS.** System type (SF Pro, semantic styles), semantic colors,
   SF Symbols, standard gestures, standard navigation (`NavigationStack`,
   `TabView`, `.searchable`, sheets with detents), full Dynamic Type and Dark Mode.
2. **One truth.** The relay's event stream — *who / when / how / which door* — drives
   everything. Attribution is the product.
3. **Glanceable first.** Widgets, notifications, Live Activities and Control
   Center are first-class surfaces, not afterthoughts.
4. **Private by default.** No accounts, no vendor cloud. Local. Face ID for
   unlock. The only external hop is Apple Push (APNs).
5. **Discipline.** Few features, finished. Everything else is opt-in and fails safe.

## Information architecture

Three tabs on iPhone, split view on iPad.

- **Home** — the doors as hero objects: live status, a large Lock/Unlock action,
  the latest event, and a presence row (who is home, derived from attributed
  events). Handles N doors.
- **History** — a timeline grouped by day, filterable by door, person and method.
- **Settings** — people and roles, per-person notification preferences, quiet
  hours, Face ID, relay, appearance, diagnostics, about.

iPad: `NavigationSplitView` (sidebar: doors + people; detail: a door).

## Design system

- **Color.** Semantic system colors plus one accent (the app tint). Status is
  never colour alone: locked = secondary, unlocked = accent, error = system red;
  always paired with an SF Symbol and text.
- **Type.** SF Pro via semantic text styles; Dynamic Type to AX5.
- **Material.** `.regularMaterial` / `.bar` for overlays; `.background(.background)`.
- **Layout.** 8 pt base grid; corner radii 10–16; generous whitespace.
- **Motion.** Springs, `matchedGeometryEffect`, SF Symbol effects
  (`.variableColor`, `.bounce`). Reduce Motion falls back to a cross-fade.
- **Haptics.** `.sensoryFeedback` for unlock (success), lock (impact) and errors.

## Surfaces

- **Widgets (WidgetKit).** Home (small/medium/large), Lock Screen
  (accessory circular/rectangular/inline) and StandBy. Interactive (iOS 17+):
  Lock/Unlock via App Intent directly in the widget.
- **Live Activities + Dynamic Island.** While a door is unlocked/open: compact =
  state, expanded = last person + Lock button.
- **Control Center / Lock Screen controls (iOS 18).** A Lock/Unlock control.
- **Siri · App Intents · Shortcuts · Spotlight · Action button.** `LockDoor`,
  `UnlockDoor` (`.authenticationRequired`), `WhoCameHomeLast`.
- **Apple Watch.** App, complication and one-tap unlock with a haptic confirmation.

## Screens and states

**Home**
- A door hero: large lock state, animated on change; a large Lock/Unlock target
  (≥44 pt); a secondary "door open/closed" row when a door sensor exists.
- Optimistic UI: immediate haptic + a "working" state, confirmed by the real
  state, with a clear, recoverable banner on failure.
- Presence row: a monogram per person, Home/Away derived from the latest
  attributed event.
- States: **offline** (relay/HA down) dims the surface and explains how to retry;
  **unknown** ("unattributed"); **first run** guides pairing.

**History**
- `.listStyle(.insetGrouped)`, day headers, rows with a method SF Symbol, a
  monogram and a time; `.contextMenu` (copy, details); `.refreshable`; a friendly
  empty state; searchable; a filter menu in the toolbar.

**Settings**
- `Form` / `List`, system standard. Face ID toggle, per-person preferences,
  quiet hours, relay status, diagnostics.

## Accessibility & localization

VoiceOver labels/hints/traits, Dynamic Type, Increase Contrast, Reduce
Transparency/Motion, Switch and Voice Control, hit targets ≥44 pt. Swedish and
English with correct pluralization; dates and times via `FormatStyle`.

## Edge states (designed, not left to chance)

No network · relay down · HA down · command in flight · lock jammed · unknown
person · empty history · first run · multiple doors · door sensor absent.

## Explicitly out of scope for v1

Custom themes beyond accent, manual history editing, accounts, cloud features,
and any feature that does not serve *who / when / how* or lock control.
