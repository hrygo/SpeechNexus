#!/usr/bin/env node

/**
 * SpeechNexus Design Token & Architectural Boundary Validator
 *
 * This script enforces:
 * 1. Exact Token Parity: Every CSS variable referenced in tokens.json and tokens/index.ts
 *    must have an exact declaration (--var-name:) in styles.css (no substring false positives).
 * 2. No Hardcoded Colors: Intercepts raw hex (#...), rgb/rgba(...), hsl/hsla(...), or oklch(...)
 *    literals in TSX/TS components (including brand-mark.tsx and tokens/index.ts).
 * 3. Architectural Copy Guard: Forbids misleading "private/admin-only" terms in shared deployment.
 * 4. Capability Tri-State Integrity: Restricts states to "ready" | "contract" | "planned"
 *    (supporting both string attributes and JSX expression containers).
 * 5. No Mock/Random Telemetry: Prohibits Math.random() in production UI components.
 * 6. Air-Gap External Asset Guard: Prohibits external http(s):// fonts, stylesheets, images,
 *    or scripts across web HTML, CSS, SVG, and TS/TSX files (allowing only W3C XML namespaces).
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, "../..");

export const FORBIDDEN_COPY = [
  "仅自己可见",
  "私有空间",
  "个人专属",
  "仅管理员可见",
  "仅管理员可操作",
];

export const VALID_CAPABILITY_STATES = new Set(["ready", "contract", "planned"]);

const ALLOWED_EXTERNAL_URLS = new Set([
  "http://www.w3.org/2000/svg",
  "http://www.w3.org/1999/xlink",
]);

// Only styles.css and tokens.json define raw primitive color values; all TS/TSX files must use CSS variables
const EXEMPT_COLOR_FILES = new Set([
  path.join(repoRoot, "web/shared/src/ui/styles.css"),
  path.join(repoRoot, "web/shared/src/tokens/tokens.json"),
]);

export function extractDefinedCssVariables(cssContent) {
  const definedVars = new Set();
  for (const match of cssContent.matchAll(/(--[a-zA-Z0-9_-]+)\s*:/g)) {
    definedVars.add(match[1]);
  }
  return definedVars;
}

export function extractReferencedCssVariables(content) {
  const referencedVars = new Set();
  for (const match of content.matchAll(/var\((--[a-zA-Z0-9_-]+)\)/g)) {
    referencedVars.add(match[1]);
  }
  return referencedVars;
}

export function validateSourceLine(
  line,
  { isExemptColorFile = false, isTestFile = false, isMarkupOrStyleOnly = false } = {},
) {
  const violations = [];
  const isComment = /^\s*(\/\/|\/\*|\*|<!--)/.test(line);

  // Rule 6: Air-Gap External Asset Guard (applies to HTML, CSS, SVG, TS, TSX)
  if (!isComment && !isTestFile) {
    for (const urlMatch of line.matchAll(/https?:\/\/[^\s"'`)>]+/gi)) {
      const url = urlMatch[0];
      const isLoopback = /^https?:\/\/(?:127\.0\.0\.1|localhost)(?::\d+)?(?:\/|$)/i.test(url);
      if (!ALLOWED_EXTERNAL_URLS.has(url) && !isLoopback) {
        violations.push({
          rule: "no-external-network-asset",
          message: `External URL '${url}' detected. SpeechNexus web assets must remain 100% air-gapped with zero external network dependencies.`,
        });
      }
    }
  }

  if (isMarkupOrStyleOnly) {
    return violations;
  }

  // Rule 2: Hardcoded Color Literals (#hex, rgb, rgba, hsl, hsla, oklch)
  if (!isExemptColorFile && !isTestFile) {
    const isHref = /href\s*=\s*["']#/.test(line);

    if (!isHref && !isComment) {
      const hexMatch = /#([0-9a-fA-F]{3,8})\b/.exec(line);
      const funcColorMatch = /\b(rgba?|hsla?|oklch)\s*\(/i.exec(line);
      const matchedLiteral = hexMatch ? hexMatch[0] : funcColorMatch ? funcColorMatch[0] : null;
      if (matchedLiteral) {
        violations.push({
          rule: "no-hardcoded-color",
          message: `Hardcoded color '${matchedLiteral}' detected. Components must use design tokens (--nexus-* or semantic CSS variables).`,
        });
      }
    }
  }

  // Rule 3: Architectural Boundary Copy Guard
  if (!isTestFile) {
    for (const phrase of FORBIDDEN_COPY) {
      if (line.includes(phrase)) {
        violations.push({
          rule: "architectural-copy-guard",
          message: `Prohibited misleading copy '${phrase}' detected. SpeechNexus P0 is a shared deployment with no user/tenant isolation. Mark resources as '当前部署共享'.`,
        });
      }
    }
  }

  // Rule 4: Capability State Integrity (matches state="x", state={'x'}, state={"x"}, state={`x`})
  if (!isTestFile) {
    const stateMatch = line.match(/\b(?:state|statusState)\s*=\s*(?:\{\s*)?["'`]([^"'`]+)["'`]/);
    if (stateMatch) {
      const stateValue = stateMatch[1];
      if (!VALID_CAPABILITY_STATES.has(stateValue)) {
        violations.push({
          rule: "capability-state-integrity",
          message: `Invalid capability state '${stateValue}'. Must strictly be one of: 'ready' | 'contract' | 'planned'.`,
        });
      }
    }
  }

  // Rule 5: No Math.random() in production UI
  if (line.includes("Math.random()") && !isTestFile) {
    violations.push({
      rule: "no-fake-telemetry",
      message:
        "Math.random() is forbidden in production UI components. Do not fake metrics, latency, or GPU telemetry.",
    });
  }

  return violations;
}

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

export function runValidation(rootDir = repoRoot) {
  const errors = [];

  const tokensJsonPath = path.join(rootDir, "web/shared/src/tokens/tokens.json");
  const tokensTsPath = path.join(rootDir, "web/shared/src/tokens/index.ts");
  const stylesPath = path.join(rootDir, "web/shared/src/ui/styles.css");

  if (
    !fs.existsSync(tokensJsonPath) ||
    !fs.existsSync(tokensTsPath) ||
    !fs.existsSync(stylesPath)
  ) {
    throw new Error("Missing tokens.json, tokens/index.ts, or styles.css");
  }

  const stylesContent = fs.readFileSync(stylesPath, "utf-8");
  const definedCssVars = extractDefinedCssVariables(stylesContent);
  const tokensJsonContent = fs.readFileSync(tokensJsonPath, "utf-8");
  const tokensTsContent = fs.readFileSync(tokensTsPath, "utf-8");

  const jsonVars = extractReferencedCssVariables(tokensJsonContent);
  const tsVars = extractReferencedCssVariables(tokensTsContent);

  // Check 1a: Every CSS variable referenced in tokens.json and tokens/index.ts must exist in styles.css
  for (const [tokenFile, varSet] of [
    [tokensJsonPath, jsonVars],
    [tokensTsPath, tsVars],
  ]) {
    for (const cssVar of varSet) {
      if (!definedCssVars.has(cssVar)) {
        errors.push({
          file: tokenFile,
          line: 1,
          message: `Token references CSS variable '${cssVar}', but it is not declared in styles.css.`,
          snippet: cssVar,
        });
      }
    }
  }

  // Check 1b: Every token variable exported in tokens/index.ts must also be registered in tokens.json
  for (const tsVar of tsVars) {
    if (!tsVar.startsWith("--nexus-color-") && !jsonVars.has(tsVar)) {
      errors.push({
        file: tokensTsPath,
        line: 1,
        message: `Token '${tsVar}' in tokens/index.ts is missing from the W3C DTCG registry (tokens.json).`,
        snippet: tsVar,
      });
    }
  }

  // Check 2-6: Scan all web source and asset files
  const webRoot = path.join(rootDir, "web");
  const files = getFilesRecursively(webRoot, /\.(tsx?|css|html|svg)$/);

  for (const file of files) {
    const content = fs.readFileSync(file, "utf-8");
    const lines = content.split("\n");
    const isExemptColorFile = EXEMPT_COLOR_FILES.has(file);
    const isTestFile = file.includes(".test.");
    const isMarkupOrStyleOnly = /\.(html|svg)$/.test(file);

    for (let i = 0; i < lines.length; i++) {
      const lineNum = i + 1;
      const line = lines[i];

      const violations = validateSourceLine(line, {
        isExemptColorFile,
        isTestFile,
        isMarkupOrStyleOnly,
      });
      for (const v of violations) {
        errors.push({
          file,
          line: lineNum,
          message: v.message,
          snippet: line,
        });
      }
    }
  }

  return errors;
}

if (process.argv[1] && path.resolve(process.argv[1]) === __filename) {
  console.log("Checking SpeechNexus design tokens and architectural boundaries...");
  const errors = runValidation(repoRoot);

  for (const err of errors) {
    const relPath = path.relative(repoRoot, err.file);
    console.error(`\x1b[31m[DESIGN-LINT-ERROR]\x1b[0m ${relPath}:${err.line}`);
    console.error(`  \x1b[1mRule Violation:\x1b[0m ${err.message}`);
    if (err.snippet) {
      console.error(`  \x1b[2mSnippet:\x1b[0m ${err.snippet.trim()}`);
    }
    console.error("");
  }

  if (errors.length > 0) {
    console.error(
      `\x1b[31m[FAILED]\x1b[0m Design token validation failed with ${errors.length} error(s).`,
    );
    process.exit(1);
  } else {
    console.log(
      "\x1b[32m[PASSED]\x1b[0m Design tokens, copy boundaries, and capability states validated successfully.",
    );
    process.exit(0);
  }
}
