#!/usr/bin/env node

/**
 * SpeechNexus Design Token & Architectural Boundary Validator
 *
 * This script enforces:
 * 1. Registry shape, primitive value parity, CSS reference closure and same-rule alias cycles.
 * 2. Color literals in Web scripts, CSS, HTML and SVG; raw shared custom properties
 *    and explicitly hash-pinned static assets have narrow, reviewed exemptions.
 * 3. Architectural Copy Guard: Forbids misleading "private/admin-only" terms in shared deployment.
 * 4. Capability Tri-State Integrity: Restricts states to "ready" | "contract" | "planned"
 *    (supporting both string attributes and JSX expression containers).
 * 5. No Mock/Random Telemetry: Prohibits Math.random() in production UI components.
 * 6. Air-Gap External Asset Guard: Prohibits external http(s):// fonts, stylesheets, images,
 *    or scripts across web HTML, CSS, SVG, and TS/TSX files (allowing only W3C XML namespaces).
 */

import {
  withoutComments,
  references,
  declarations,
  hardcodedColors,
  validateRegistry,
  aliasCycles,
} from "./design-token-integrity.mjs";
import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
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

export function extractDefinedCssVariables(cssContent) {
  return new Set(declarations(cssContent).map((match) => match[1]));
}

export function extractReferencedCssVariables(content) {
  return references(content);
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

  if (!isExemptColorFile && !isTestFile && !isComment) {
    for (const color of hardcodedColors(line)) {
      violations.push({
        rule: "no-hardcoded-color",
        message: `Hardcoded color '${color}' detected. Use design tokens.`,
      });
    }
  }
  if (isMarkupOrStyleOnly) return violations;

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

  const { registered, errors: registryErrors } = validateRegistry(tokensJsonContent, stylesContent);
  for (const message of registryErrors) errors.push({ file: tokensJsonPath, line: 1, message });
  const tsVars = references(withoutComments(tokensTsContent, { script: true }));
  for (const cssVar of registered) {
    if (!definedCssVars.has(cssVar))
      errors.push({
        file: tokensJsonPath,
        line: 1,
        message: `Token references undefined CSS variable '${cssVar}'.`,
      });
  }
  for (const tsVar of tsVars) {
    if (!registered.has(tsVar))
      errors.push({
        file: tokensTsPath,
        line: 1,
        message: `Token '${tsVar}' is missing from the project registry (tokens.json).`,
      });
  }

  // Check 2-6: Scan all web source and asset files
  const webRoot = path.join(rootDir, "web");
  const files = getFilesRecursively(webRoot, /\.([cm]?[jt]sx?|css|html|svg)$/);
  const exceptionPath = path.join(rootDir, "web/shared/design-token-exceptions.json");
  const exceptions = fs.existsSync(exceptionPath)
    ? JSON.parse(fs.readFileSync(exceptionPath, "utf8"))
    : {};
  for (const [name, entry] of Object.entries(exceptions)) {
    const target = path.resolve(rootDir, name);
    if (
      !name.startsWith("web/") ||
      path.relative(rootDir, target).startsWith("..") ||
      !entry.reason ||
      !/^[a-f0-9]{64}$/.test(entry.sha256 ?? "") ||
      !fs.existsSync(target)
    ) {
      errors.push({
        file: exceptionPath,
        line: 1,
        message: `Invalid design token exception '${name}'.`,
      });
    } else if (
      createHash("sha256").update(fs.readFileSync(target)).digest("hex") !== entry.sha256
    ) {
      errors.push({
        file: target,
        line: 1,
        message:
          "Design token exception content changed; review its literals and update the pinned hash.",
      });
    }
  }

  for (const file of files) {
    const isScript = /\.[cm]?[jt]sx?$/.test(file);
    const content = withoutComments(fs.readFileSync(file, "utf-8"), { script: isScript });
    const lines = content.split("\n");
    const isTestFile = /(?:\.test\.|\.spec\.|[/\\]__tests__[/\\])/.test(file);
    if (isTestFile) continue;
    const isPalette = file === stylesPath;
    const isCss = file.endsWith(".css");
    const isMarkupOrStyleOnly = /\.(html|svg)$/.test(file);
    if (isCss) {
      for (const cycle of aliasCycles(content))
        errors.push({ file, line: 1, message: `CSS alias cycle: ${cycle}` });
    }
    for (const cssVar of references(content)) {
      if (!definedCssVars.has(cssVar))
        errors.push({
          file,
          line: content.slice(0, content.indexOf(cssVar)).split("\n").length,
          message: `Undefined CSS variable '${cssVar}'.`,
        });
    }
    const approvedAsset = Object.hasOwn(
      exceptions,
      path.relative(rootDir, file).split(path.sep).join("/"),
    );
    for (const color of approvedAsset
      ? []
      : hardcodedColors(content, { css: isCss, palette: isPalette })) {
      errors.push({
        file,
        line: content.slice(0, content.indexOf(color)).split("\n").length,
        message: `Hardcoded color '${color}' detected. Use design tokens; raw values belong in shared CSS custom properties.`,
      });
    }

    for (let i = 0; i < lines.length; i++) {
      const lineNum = i + 1;
      const line = lines[i];

      const violations = validateSourceLine(line, {
        isExemptColorFile: true,
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
