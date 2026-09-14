# Accessibility audit: DiffContext documentation site

Conformance target: **WCAG 2.2 Level AA**
Tooling: **axe-core 4.13.0**, driven by Playwright against Chromium
Scope: the 10 statically exported pages of `website/`, tested in both the light
and the dark theme (20 page-theme combinations)
Date: September 2026

Reproduce with:

```bash
cd website
npm ci && npm run build
npm run a11y
```

The script builds nothing itself. It serves `out/` and runs axe against every
page in both themes, so the numbers below can be regenerated from a clean
checkout.

---

## Result

| | Violation instances | Critical | Serious |
|---|---|---|---|
| Before | **66** | 18 | 48 |
| After | **0** | 0 | 0 |

Three axe rules accounted for all 66 instances.

| Rule | Instances | Impact | WCAG SC |
|---|---|---|---|
| `color-contrast` | 46 | serious | 1.4.3 Contrast (Minimum) |
| `button-name` | 18 | critical | 4.1.2 Name, Role, Value |
| `scrollable-region-focusable` | 2 | serious | 2.1.1 Keyboard, 2.1.3 Keyboard (No Exception) |

Violations were not spread evenly. The dark theme carried 44 of the 66
instances against 22 in light, almost entirely because one syntax-highlighting
token sits just below threshold on a black background and repeats on every
page that contains a code block.

---

## 1. `button-name` — 18 instances, critical

**Symptom.** One control on every page exposed no accessible name at all. axe
reported no inner text, no `aria-label`, no `aria-labelledby`, no `title`.

```html
<button class="x:cursor-pointer x:h-7 ... x:rounded-none"
        id="headlessui-listbox-button-..."
        type="button" aria-haspopup="listbox" aria-expanded="false">
```

**Root cause.** This is the dropdown half of the "Copy page" split button,
rendered by `nextra-theme-docs`. Its sibling `ThemeSwitch` passes
`title="Change theme"` into the shared `Select` component, which forwards
`title` to the Headless UI `ListboxButton`. The `CopyPage` component omits
that prop, so the trigger ships with no name. A screen reader announces it as
"button, collapsed" with nothing to say what it does.

**Fix.** Pass the missing prop, applied through `patch-package` since the
defect is upstream:

```js
jsx(Select, { anchor: t6, title: "More page actions", className: "x:rounded-none", ... })
```

See `website/patches/nextra-theme-docs+4.6.1.patch`. This is worth sending
upstream.

---

## 2. `color-contrast` — 46 instances, serious

Five distinct colour pairs failed, all of them close to the line. Nothing here
was visibly broken, which is the point: contrast failures in this range are
invisible to someone with typical vision and decisive for someone without.

| Context | Foreground | Background | Measured | Required |
|---|---|---|---|---|
| Shiki comment token, dark | `#6A737D` | `#000000` | 4.36:1 | 4.5:1 |
| Shiki constant token, light | `#E36209` | `#FFFFFF` | 3.48:1 | 4.5:1 |
| Inline code in warning callout, light | `#A65F00` | `#F6F4E1` | 4.44:1 | 4.5:1 |
| Inline code, light | `#006BE6` | `#F3F3F3` | 4.45:1 | 4.5:1 |
| In-page anchor, light | `#006BE6` | `#F3F4F6` | 4.49:1 | 4.5:1 |
| Body copy in error callout, dark | `#FB2C36` | `#331314` | 4.43:1 | 4.5:1 |

**Fix.** Overrides in `website/app/globals.css`, each annotated with the ratio
that justified it. The two `#006BE6` failures share a cause, so they share a
fix: dropping `--nextra-primary-lightness` to 38% in light mode lifts both the
inline code and the in-page anchors without touching hue or the dark theme.

No visual redesign was involved. Every change is a shift in lightness along an
existing hue.

---

## 3. `scrollable-region-focusable` — 2 instances, serious

**Symptom.** The pipeline diagram in `components/Pipeline.jsx` is a
`min-w-[800px]` row inside an `overflow-x-auto` wrapper. It scrolls
horizontally with a mouse or trackpad and was unreachable by keyboard, so a
keyboard-only user could not read the right-hand half of the diagram at all.

**Fix.** The wrapper takes a tab stop and an accessible name:

```jsx
<div className="w-full my-12 overflow-x-auto pb-4"
     tabIndex={0}
     role="group"
     aria-label="DiffContext pipeline stages, scrollable horizontally">
```

The five step icons inside it are decorative, each already paired with a
visible text label, so they take `aria-hidden="true"` to stop screen readers
announcing five unnamed graphics before the labels they duplicate.

---

## What this audit does not cover

Stated plainly, because an audit that overstates its scope is worse than no
audit.

- **Automated testing only.** axe-core catches roughly a third of WCAG issues.
  A clean axe run is a floor, not conformance.
- **No manual screen reader pass.** Nothing here has been verified against
  NVDA, JAWS or VoiceOver.
- **No keyboard walkthrough** beyond the one rule axe flags.
- **Not checked:** focus order, focus visibility against the 2.4.11 criteria,
  reflow at 320px, zoom to 200%, target size (2.5.8), motion preferences.

Those are the next pass.
