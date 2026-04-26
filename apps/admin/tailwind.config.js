/** @type {import('tailwindcss').Config} */

/*
 * Omniscience admin — semantic color palette.
 *
 * The admin UI ships with **dark theme as default** (Issue #110). A future
 * follow-up will introduce a light theme + toggle. To keep that follow-up
 * cheap, every token is defined ONCE here as a Tailwind color name that
 * resolves to a CSS variable. The variable's value is set in `index.css`:
 * `:root` holds the light theme, `:root.dark` overrides for dark theme.
 *
 * Why CSS variables and not the `dark:` Tailwind variant on every class?
 * ---------------------------------------------------------------------
 *   - Components stay token-only: `bg-surface`, never `bg-white dark:bg-zinc-950`.
 *   - Adding a third theme (high-contrast, sepia, ...) is a CSS change, not
 *     a sweep of every component file.
 *   - The semantic name is the API; the hex values are an implementation detail.
 *
 * Conventions
 * -----------
 *   - Neutrals: zinc-derived (cooler than stone, less blue than slate). The
 *     operator UI is dense and text-heavy; cool greys read calmer than warm
 *     ones over long sessions.
 *   - Accent: indigo (kept from the v0.1 admin UI to avoid breaking muscle
 *     memory). Used sparingly — primary buttons, selected nav, links, focus
 *     ring.
 *   - Status: success/warning/danger/info. Two intensities each:
 *       - `*-fg`  — foreground (text/icon)
 *       - `*-bg`  — background tint for badges and banners
 *   - Charts: chart-1 .. chart-6, hand-tuned for ~AAA contrast on the
 *     `surface` background and visible separation in dark mode.
 *
 * Contrast targets (verified against the dark values in index.css)
 * ----------------------------------------------------------------
 *   - text on surface         : >= 7:1   (WCAG AAA body text)
 *   - text-muted on surface   : >= 4.5:1 (WCAG AA body)
 *   - status-fg on status-bg  : >= 4.5:1 (WCAG AA non-text components)
 *   - accent on surface       : >= 4.5:1 (focus/links)
 *
 * Adding a token
 * --------------
 *   1) Add the Tailwind name below pointing at `var(--token-name)`.
 *   2) Set both light (`:root`) and dark (`:root.dark`) values in index.css.
 *   3) Document its purpose, light value, dark value, and contrast intent.
 *   4) Use the token in components — NEVER raw hex, NEVER Tailwind defaults.
 */

const tok = (name) => `var(--${name})`;

export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // Surfaces
        surface: tok("surface"),
        "elevation-1": tok("elevation-1"),
        "elevation-2": tok("elevation-2"),

        // Borders
        border: tok("border"),
        "border-strong": tok("border-strong"),

        // Text
        text: tok("text"),
        "text-secondary": tok("text-secondary"),
        "text-muted": tok("text-muted"),
        "text-inverse": tok("text-inverse"),

        // Accent (primary action)
        accent: tok("accent"),
        "accent-hover": tok("accent-hover"),
        "accent-bg": tok("accent-bg"),
        "accent-fg": tok("accent-fg"),

        // Status pairs
        "success-fg": tok("success-fg"),
        "success-bg": tok("success-bg"),
        "warning-fg": tok("warning-fg"),
        "warning-bg": tok("warning-bg"),
        "warning-border": tok("warning-border"),
        "danger-fg": tok("danger-fg"),
        "danger-bg": tok("danger-bg"),
        "danger-border": tok("danger-border"),
        "danger-strong": tok("danger-strong"),
        "danger-strong-hover": tok("danger-strong-hover"),
        "info-fg": tok("info-fg"),
        "info-bg": tok("info-bg"),

        // Overlay (dialog backdrop)
        overlay: tok("overlay"),

        // Chart palette
        "chart-1": tok("chart-1"),
        "chart-2": tok("chart-2"),
        "chart-3": tok("chart-3"),
        "chart-4": tok("chart-4"),
        "chart-5": tok("chart-5"),
        "chart-6": tok("chart-6"),
      },
      ringColor: {
        DEFAULT: tok("accent"),
      },
    },
  },
  plugins: [],
};
