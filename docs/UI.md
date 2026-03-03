# UI

## Purpose

The UI is not a separate demo theme. It is the actual application shell and should be reusable as a base for PoCs and MVPs.

The same visual language should apply to:

- core pages
- admin pages
- add-on user pages
- add-on admin pages
- the static `ui_template/`

## Shell

The main shell is defined in [base.html](/home/matteo/PycharmProjects/webApp/app/templates/base.html) and includes:

1. topbar
2. mobile top navigation
3. desktop sidebar
4. content area
5. global toasts and modals
6. language switcher

Every new page should extend `base.html`.

## Breakpoints

- `md` and above: desktop layout with sidebar
- below `md`: mobile layout with horizontal top navigation

Expected behavior:

- no sidebar overlay on small screens
- mobile navigation remains usable with many items
- `ui_template/` mirrors the same shell behavior
- mobile navigation uses a compact bar plus offcanvas drawer, not a deep always-open stack

## Key Files

- [base.html](/home/matteo/PycharmProjects/webApp/app/templates/base.html)
- [app.css](/home/matteo/PycharmProjects/webApp/app/static/css/app.css)
- [app.js](/home/matteo/PycharmProjects/webApp/app/static/js/app.js)
- [sidebar-cookie.js](/home/matteo/PycharmProjects/webApp/app/static/js/features/sidebar-cookie.js)
- [confirm-modal.js](/home/matteo/PycharmProjects/webApp/app/static/js/features/confirm-modal.js)

## UI Patterns

### Page Header

Use:

- breadcrumb
- clear title
- short description
- right-side actions only when needed

### Cards

Cards should stay compact and readable:

- settings cards
- metrics cards
- widget cards
- logs and status cards

Reference:

- the `Widget & UI` add-on is the visual source of truth for card density, header rhythm, stat cards, tabs, tables, modal treatment, and preview blocks
- pages that drift away from that grammar should be brought back to the shared app component language

### Forms

Rules:

- short labels
- compact inputs
- one obvious primary action
- confirmation only for destructive actions

### Tables

Admin tables should be:

- dense
- filterable
- readable through badges and labels
- expandable only when details add real value

Example:

- the database admin page uses dense table metrics plus a compact diagnostics console instead of oversized cards

### Feedback

Use:

- flash -> toast
- confirmation modal only for risky actions
- persistent badges and stat cards for state
- progress modals for multi-step operations such as add-on install or runtime restart

CSP rule:

- `Widget & UI` examples must not rely on inline `style=` attributes or inline event handlers
- demo interactions should use nonce-backed `<style>` / `<script>` blocks plus classes and `data-*` hooks
- if a component cannot survive `style-src-attr 'none'` and `script-src-attr 'none'`, it is not a valid reference implementation

## Typography

Standard UI pages use:

- sans-serif for shell and app controls
- compact sizing
- clear hierarchy without oversized headings

Explicit exception:

- the `documentation` add-on uses serif body text and monospace code blocks for long-form reading

## Localization

The UI layer supports:

- `en` as default
- `it` as alternate language

Implementation notes:

- language resolution is request-aware and session-aware
- shell-level labels should go through the shared translation helper
- standalone `ui_template/` is shipped in English

## Charts and Widgets

The `Widget & UI` add-on is the visual catalog for reusable UI patterns.

It should cover:

- stat cards
- line charts
- donut/progress visuals
- stacked bars
- graph/dependency maps
- UI config panels

Operational rule:

- `Widget & UI` is not just a demo board; admin pages such as `Config WebApp` and add-on install flows should stay visually aligned with its modal, card, and progress patterns

## Add-on UI Rules

Each add-on should:

- provide at least one user view
- optionally provide an admin view
- inherit the app shell
- reuse the same spacing, density, and interaction patterns

Access rule:

- users see user pages
- admins can access both user and admin pages

Navigation rule:

- user add-on pages appear in normal navigation
- add-on admin pages are selected from inside the add-on page itself and should not appear in the global sidebar/topbar

## Static Template

[ui_template](/home/matteo/PycharmProjects/webApp/ui_template) must stay a faithful static copy of the real application UI.

It should not become:

- a disconnected demo
- a parallel theme
- a redesign experiment

Practical maintenance rule:

- when shared shell or component CSS changes in the real app, `ui_template/static/css/app.css` must be updated to match
- if the app UI and `ui_template/` disagree, the app is the source of truth
