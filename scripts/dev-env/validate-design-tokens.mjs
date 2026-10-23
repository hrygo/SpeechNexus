#!/usr/bin/env node

/**
 * SpeechNexus Design Token & Architectural Boundary Validator
 *
 * This script enforces:
 * 1. Token Parity: All CSS variables referenced in tokens.json exist in styles.css
 * 2. No Hardcoded Colors: Intercepts raw hex (#...) or rgb/hsl colors in TSX/TS components
 * 3. Architectural Copy Guard: Forbids misleading "private/admin-only" terms in shared deployment
 * 4. Capability Tri-State Integrity: Restricts states to "ready" | "contract" | "planned"
 * 5. No Mock/Random Telemetry: Prohibits Math.random() in production UI components
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, "../..");

let errorCount = 0;

function reportError(file, line, message, snippet) {
  errorCount++;
  const relPath = path.relative(repoRoot, file);
  console.error(`\x1b[31m[DESIGN-LINT-ERROR]\x1b[0m ${relPath}:${line}`);
  console.error(`  \x1b[1mRule Violation:\x1b[0m ${message}`);
  if (snippet) {
    console.error(`  \x1b[2mSnippet:\x1b[0m ${snippet.trim()}`);
  }
  console.error("");
}

// -----------------------------------------------------------------------------
// Check 1: Design Token & CSS Variable Parity
// -----------------------------------------------------------------------------
function checkTokenParity() {
  const tokensPath = path.join(repoRoot, "web/shared/src/tokens/tokens.json");
  const stylesPath = path.join(repoRoot, "web/shared/src/ui/styles.css");

  if (!fs.existsSync(tokensPath) || !fs.existsSync(stylesPath)) {
    console.error("\x1b[31m[ERROR]\x1b[0m Missing tokens.json or styles.css");
    process.exit(1);
  }

  const tokensContent = fs.readFileSync(tokensPath, "utf-8");
  const stylesContent = fs.readFileSync(stylesPath, "utf-8");

  // Extract all var(--xxx) from tokens.json
  const varMatches = [...tokensContent.matchAll(/var\((--[a-zA-Z0-9_-]+)\)/g)];
  const tokenVars = new Set(varMatches.map((m) => m[1]));

  for (const cssVar of tokenVars) {
    if (!stylesContent.includes(cssVar + ":") && !stylesContent.includes(cssVar)) {
      reportError(
        tokensPath,
        1,
        `Token references CSS variable '${cssVar}', but it is not defined in styles.css.`,
        cssVar
      );
    }
  }
}

// -----------------------------------------------------------------------------
// Check 2: Scans web component files for hardcoded styling and boundaries
// -----------------------------------------------------------------------------
const FORBIDDEN_COPY = [
  "仅自己可见",
  "私有空间",
  "个人专属",
  "仅管理员可见",
  "仅管理员可操作",
];

const VALID_CAPABILITY_STATES = new Set(["ready", "contract", "planned"]);

// Files exempt from hardcoded color check (e.g., brand SVG icon fallback definition, styles)
const EXEMPT_COLOR_FILES = new Set([
  path.join(repoRoot, "web/shared/src/ui/styles.css"),
  path.join(repoRoot, "web/shared/src/tokens/tokens.json"),
  path.join(repoRoot, "web/shared/src/tokens/index.ts"),
  path.join(repoRoot, "web/shared/src/ui/brand-mark.tsx"),
]);

function getFilesRecursively(dir, filterRegex) {
  const results = [];
  if (!fs.existsSync(dir)) return results;
  const list = fs.readdirSync(dir);
  for (const file of list) {
    const fullPath = path.join(dir, file);
    const stat = fs.statSync(fullPath);
    if (stat.isDirectory()) {
      if (file !== "node_modules" && file !== "dist" && file !== "coverage") {
        results.push(...getFilesRecursively(fullPath, filterRegex));
      }
    } else if (filterRegex.test(fullPath)) {
      results.push(fullPath);
    }
  }
  return results;
}

function checkWebComponents() {
  const dirs = [
    path.join(repoRoot, "web/admin/src"),
    path.join(repoRoot, "web/workbench/src"),
    path.join(repoRoot, "web/shared/src/ui"),
  ];

  const files = [];
  for (const d of dirs) {
    files.push(...getFilesRecursively(d, /\.(tsx?|css)$/));
  }

  // Regex to detect hex color literals (#fff, #18201c, etc.) but avoid href="#id" or #!/
  const hexColorRegex = /#([0-9a-fA-F]{3,8})\b/;

  for (const file of files) {
    const content = fs.readFileSync(file, "utf-8");
    const lines = content.split("\n");

    for (let i = 0; i < lines.length; i++) {
      const lineNum = i + 1;
      const line = lines[i];

      // Rule 2: Hardcoded Hex Colors
      if (!EXEMPT_COLOR_FILES.has(file)) {
        // Skip href="#..." or imports or comments
        const isHref = /href\s*=\s*["']#/.test(line);
        const isComment = /^\s*(\/\/|\/\*|\*)/.test(line);

        if (!isHref && !isComment) {
          const match = hexColorRegex.exec(line);
          if (match) {
            reportError(
              file,
              lineNum,
              `Hardcoded hex color '${match[0]}' detected. Components must use design tokens (--nexus-* or semantic CSS variables).`,
              line
            );
          }
        }
      }

      // Rule 3: Architectural Boundary Copy Guard
      for (const phrase of FORBIDDEN_COPY) {
        if (line.includes(phrase)) {
          reportError(
            file,
            lineNum,
            `Prohibited misleading copy '${phrase}' detected. SpeechNexus P0 is a shared deployment with no user/tenant isolation. Mark resources as '当前部署共享'.`,
            line
          );
        }
      }

      // Rule 4: Capability State Integrity
      const stateMatch = line.match(/state\s*=\s*["']([^"']+)["']/);
      if (stateMatch) {
        const stateValue = stateMatch[1];
        if (!VALID_CAPABILITY_STATES.has(stateValue)) {
          reportError(
            file,
            lineNum,
            `Invalid capability state '${stateValue}'. Must strictly be one of: 'ready' | 'contract' | 'planned'.`,
            line
          );
        }
      }

      // Rule 5: No Math.random() in production UI
      if (line.includes("Math.random()") && !file.includes(".test.")) {
        reportError(
          file,
          lineNum,
          "Math.random() is forbidden in production UI components. Do not fake metrics, latency, or GPU telemetry.",
          line
        );
      }
    }
  }
}

// -----------------------------------------------------------------------------
// Main Execution
// -----------------------------------------------------------------------------
console.log("Checking SpeechNexus design tokens and architectural boundaries...");
checkTokenParity();
checkWebComponents();

if (errorCount > 0) {
  console.error(
    `\x1b[31m[FAILED]\x1b[0m Design token validation failed with ${errorCount} error(s).`
  );
  process.exit(1);
} else {
  console.log(
    "\x1b[32m[PASSED]\x1b[0m Design tokens, copy boundaries, and capability states validated successfully."
  );
  process.exit(0);
}
