#!/usr/bin/env node
/*
 * apps/admin/scripts/check-no-raw-colors.mjs
 *
 * Regression guard for Issue #110 (dark theme as default).
 *
 * The admin UI uses a semantic Tailwind palette declared in
 * `tailwind.config.js` (and CSS variables in `src/index.css`). Components
 * MUST go through these tokens — `bg-surface`, `text-text-secondary`,
 * `border-border`, etc — instead of:
 *
 *   1. Tailwind built-in palette names like `bg-white`, `text-gray-900`,
 *      `border-gray-200`, `bg-indigo-600`. These bypass the theme system
 *      and lock components to a specific color regardless of which theme
 *      is active.
 *   2. Raw hex literals (`#fff`, `text-[#abc]`) anywhere in component
 *      source — same reason as above.
 *
 * Light theme + toggle is a follow-up issue. This script keeps the
 * worktree clean in the meantime so when that toggle lands, every
 * component follows the active theme automatically.
 *
 * Scope of the check:
 *   - Only `src/**` is scanned. `tailwind.config.js`, `src/index.css`, and
 *     `scripts/**` are EXEMPT — they are where the palette is defined and
 *     audited. Hex values are legal there.
 *
 * Usage:
 *   node apps/admin/scripts/check-no-raw-colors.mjs
 *   npm --prefix apps/admin run lint:colors
 *
 * Exit codes:
 *   0 — clean
 *   1 — at least one violation found
 *   2 — scan-time error (e.g. no files scanned, indicating a path bug)
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { resolve, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const ROOT = resolve(dirname(__filename), "..");
const SRC_DIR = join(ROOT, "src");

/*
 * Forbidden patterns. Each entry is a regex + a human-readable explanation.
 *
 * The Tailwind built-in color shortlist comes from the palette names that
 * this codebase used pre-#110. We could match all defaults, but the
 * targeted list keeps false positives away from class names like
 * `accent-accent` (Tailwind utility for the CSS `accent-color` property
 * paired with our `accent` token).
 */
const PATTERNS = [
  {
    name: "Tailwind built-in palette",
    re: /\b(?:bg|text|border|ring|divide|placeholder|from|via|to|fill|stroke|outline|caret)-(?:white|black|slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)(?:-\d{2,3})?\b/g,
    hint: "Use a semantic token (bg-surface, text-text, border-border, etc).",
  },
  {
    name: "Raw hex literal in JSX/TSX",
    // Catches Tailwind arbitrary-value syntax with hex (`text-[#abc]`,
    // `bg-[#aabbcc]`) and bare hex literals in className strings.
    re: /#[0-9a-fA-F]{3,8}\b/g,
    hint: "Define the color in tailwind.config.js + index.css and use the token.",
  },
];

/*
 * File extensions to scan. JS/TS/JSX/TSX only — config files and CSS are
 * deliberately excluded (they define the palette).
 */
const SCAN_EXTS = new Set([".tsx", ".ts", ".jsx", ".js"]);

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) {
      yield* walk(full);
    } else if (SCAN_EXTS.has(full.slice(full.lastIndexOf(".")))) {
      yield full;
    }
  }
}

let scanned = 0;
const violations = [];

/*
 * Strip JS line/block comments from a source line so issue/PR references
 * like "#110" inside docstrings don't get flagged as hex literals.
 *
 * Implementation: replace block comment content first (greedy single-line
 * stripping is sufficient — we already split on newlines), then strip
 * `//` line tails. Anything inside a continued multi-line block comment
 * is approximated by stripping leading "* " from a line.
 */
function stripComments(line) {
  let out = line;
  // Single-line block comments: /* ... */
  out = out.replace(/\/\*[^]*?\*\//g, "");
  // Line comment tails: // ...
  out = out.replace(/\/\/.*$/, "");
  // Inside a continued block: lines that begin with whitespace + "*"
  if (/^\s*\*/.test(out)) {
    out = "";
  }
  return out;
}

for (const file of walk(SRC_DIR)) {
  scanned += 1;
  const text = readFileSync(file, "utf8");
  const lines = text.split("\n");

  for (const { name, re, hint } of PATTERNS) {
    for (let i = 0; i < lines.length; i += 1) {
      const scanLine = stripComments(lines[i]);
      // Reset regex global state per line.
      re.lastIndex = 0;
      let match;
      while ((match = re.exec(scanLine)) !== null) {
        violations.push({
          file: file.slice(ROOT.length + 1),
          line: i + 1,
          column: match.index + 1,
          match: match[0],
          rule: name,
          hint,
        });
      }
    }
  }
}

if (scanned === 0) {
  console.error(
    `ERROR: scanned 0 files under ${SRC_DIR} — script is misconfigured.`
  );
  process.exit(2);
}

if (violations.length === 0) {
  console.log(
    `[check-no-raw-colors] OK — scanned ${scanned} file(s), no raw colors found.`
  );
  process.exit(0);
}

console.error(
  `[check-no-raw-colors] FAIL — ${violations.length} violation(s) in ${scanned} file(s):\n`
);
for (const v of violations) {
  console.error(
    `  ${v.file}:${v.line}:${v.column}  ${v.rule}  -> "${v.match}"\n    ${v.hint}`
  );
}
process.exit(1);
