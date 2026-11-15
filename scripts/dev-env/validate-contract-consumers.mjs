#!/usr/bin/env node

/**
 * SpeechNexus generated-contract consumer binding validator.
 *
 * `packages/contracts` is the generated boundary package. Web entries must consume
 * it through the package manifest (`@speechnexus/contracts` plus its declared
 * `exports` subpaths), not by reaching into `packages/contracts/src/**` with a
 * relative path. "Same version" then follows from the declared workspace
 * dependency and its exports, so a renamed package, a moved artifact or a
 * package removed from the workspace fails as a resolution/declaration error
 * instead of a missing file path.
 *
 * The guard is deliberately dependency-free so the `changes` job can run it
 * before installing project dependencies. It enforces four two-way rules:
 *
 * 1. Every declared `exports` target is an existing file. A generated artifact
 *    that moved without its `exports` entry fails here even though a relative
 *    path would still reach it.
 * 2. Code under `web/` must not import `packages/contracts/src/**` by relative
 *    path; imports go through `@speechnexus/contracts[/<subpath>]`.
 * 3. Every `@speechnexus/contracts[/<subpath>]` specifier maps to a declared
 *    export, and the importing workspace package declares
 *    `"@speechnexus/contracts": "workspace:*"`. Removing that dependency fails
 *    here even before TypeScript does.
 * 4. Every declared subpath has at least one such consumer, or is registered
 *    with a reason in `packages/contracts/export-consumers.json`. A new export
 *    without a consumer fails, and a stale registration (its subpath gained a
 *    consumer) fails too, so the registry cannot silently rot.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { withoutComments } from "./design-token-integrity.mjs";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, "../..");

export const CONTRACTS_PACKAGE = "packages/contracts";
export const PACKAGE_SPECIFIER = "@speechnexus/contracts";
export const REGISTRY_PATH = `${CONTRACTS_PACKAGE}/export-consumers.json`;
export const SCAN_ROOTS = ["web", "scripts", "tests", "packages"];
export const CODE_EXTENSIONS = [".ts", ".tsx", ".mts", ".cts", ".mjs", ".js"];
export const SKIP_DIRECTORIES = new Set([
  "node_modules",
  "dist",
  "coverage",
  "test-results",
  "playwright-report",
  ".runtime",
  ".venv",
  "__pycache__",
  ".git",
]);

const SPECIFIER_PATTERN = /^@speechnexus\/contracts(?:\/|$)/;
// Anchored at a statement boundary so a string literal that merely contains
// `import ... from "@speechnexus/contracts"` (fixtures, error text) is not a
// consumer; `[\s\S]*?` keeps multi-line import/export lists working, and the
// dynamic-import form is matched separately.
const IMPORT_PATTERNS = [
  // `import ... from "x"` / `export ... from "x"`, including multi-line lists.
  /(?:^|\n)[ \t]*(?:import|export)\b[\s\S]*?\bfrom\s+["']([^"']+)["']/g,
  // Bare side-effect import and dynamic import.
  /(?:^|\n)[ \t]*import\s+["']([^"']+)["']/g,
  /\bimport\s*\(\s*["']([^"']+)["']/g,
];

function toPosix(value) {
  return value.split(path.sep).join("/");
}

function lineOf(content, index) {
  return content.slice(0, index).split("\n").length;
}

/** List code files under `rootDir` for the configured scan roots. */
export function listConsumerFiles(rootDir) {
  const files = [];
  const walk = (dir) => {
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      if (entry.name.startsWith(".") || SKIP_DIRECTORIES.has(entry.name)) continue;
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (CODE_EXTENSIONS.includes(path.extname(entry.name))) files.push(full);
    }
  };
  for (const root of SCAN_ROOTS) walk(path.join(rootDir, root));
  return files.sort();
}

/** Extract static, bare and dynamic import/export specifiers from one file. */
export function extractSpecifiers(content) {
  const stripped = withoutComments(content, { script: true });
  const seen = new Set();
  const specifiers = [];
  for (const pattern of IMPORT_PATTERNS) {
    pattern.lastIndex = 0;
    for (const match of stripped.matchAll(pattern)) {
      const specifier = match[1];
      const index = match.index ?? 0;
      if (seen.has(specifier)) continue;
      seen.add(specifier);
      specifiers.push({ specifier, line: lineOf(stripped, index) });
    }
  }
  return specifiers;
}

/** Read the package manifest exports as a map of subpath key to target file. */
export function readExports(rootDir) {
  const manifestPath = path.join(rootDir, CONTRACTS_PACKAGE, "package.json");
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  const exportsField = manifest.exports;
  if (exportsField === undefined || typeof exportsField !== "object") {
    throw new Error(`${CONTRACTS_PACKAGE}/package.json must declare an exports map`);
  }
  const entries = new Map();
  for (const [key, value] of Object.entries(exportsField)) {
    if (!key.startsWith(".")) continue;
    entries.set(key, value);
  }
  return { manifestPath, entries };
}

/** Resolve the `exports` target for a declared subpath key, or null. */
export function exportTarget(exportsEntries, key) {
  const value = exportsEntries.get(key);
  if (value === undefined) return null;
  if (typeof value === "string") return value;
  if (value && typeof value === "object") {
    return value.import ?? value.types ?? value.default ?? null;
  }
  return null;
}

/** Map an import specifier to an exports subpath key (`.` for the root). */
export function subpathKey(specifier) {
  if (specifier === PACKAGE_SPECIFIER) return ".";
  if (!specifier.startsWith(`${PACKAGE_SPECIFIER}/`)) return null;
  return `./${specifier.slice(PACKAGE_SPECIFIER.length + 1)}`;
}

function readJson(file, errors, label) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch (error) {
    errors.push({ file, line: 1, message: `Cannot read ${label}: ${error.message}` });
    return null;
  }
}

/** Nearest package.json carrying a `name`, starting at `startDir`. */
export function owningPackage(rootDir, startDir) {
  let dir = startDir;
  for (;;) {
    const manifestPath = path.join(dir, "package.json");
    if (fs.existsSync(manifestPath)) {
      const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
      if (typeof manifest.name === "string") {
        return { dir, manifestPath, manifest };
      }
    }
    if (path.resolve(dir) === path.resolve(rootDir)) return null;
    const parent = path.dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

function declaredWorkspaceDependency(manifest) {
  for (const field of ["dependencies", "devDependencies", "peerDependencies"]) {
    const value = manifest?.[field]?.[PACKAGE_SPECIFIER];
    if (value !== undefined) return value;
  }
  return undefined;
}

export function runValidation(rootDir = repoRoot) {
  const errors = [];
  const contractsDir = path.join(rootDir, CONTRACTS_PACKAGE);

  // Rule 1: every declared export points at an existing generated artifact.
  const { manifestPath, entries } = readExports(rootDir);
  for (const [key, value] of entries) {
    const target = exportTarget(entries, key);
    if (target === null) {
      errors.push({
        file: manifestPath,
        line: 1,
        message: `Export '${key}' has no string import/types target.`,
      });
      continue;
    }
    const resolved = path.resolve(contractsDir, target);
    if (!resolved.startsWith(contractsDir + path.sep) || !fs.existsSync(resolved)) {
      errors.push({
        file: manifestPath,
        line: 1,
        message: `Export '${key}' targets a missing generated artifact '${target}'.`,
      });
    } else if (!fs.statSync(resolved).isFile()) {
      errors.push({
        file: manifestPath,
        line: 1,
        message: `Export '${key}' targets '${target}', which is not a file.`,
      });
    }
  }

  const registryFile = path.join(rootDir, REGISTRY_PATH);
  const registry = readJson(registryFile, errors, "the export consumer registry");
  const unboundRegistry = registry?.unbound ?? {};
  if (registry !== null && (typeof registry !== "object" || Array.isArray(registry))) {
    errors.push({ file: registryFile, line: 1, message: "Registry must be a JSON object." });
  }
  if (registry?.unbound !== undefined && typeof registry.unbound !== "object") {
    errors.push({ file: registryFile, line: 1, message: "'unbound' must be an object." });
  }
  for (const [key, reason] of Object.entries(unboundRegistry)) {
    if (typeof reason !== "string" || reason.trim() === "") {
      errors.push({
        file: registryFile,
        line: 1,
        message: `Unbound export '${key}' needs a non-empty reason.`,
      });
    }
    if (!entries.has(key)) {
      errors.push({
        file: registryFile,
        line: 1,
        message: `Registered export '${key}' is not declared in packages/contracts/package.json.`,
      });
    }
  }

  const consumers = new Map([...entries.keys()].map((key) => [key, []]));

  // Rules 2-3: consumers must bind through the manifest and declare the dependency.
  for (const file of listConsumerFiles(rootDir)) {
    const relative = toPosix(path.relative(rootDir, file));
    // The package's own tests may exercise its relative sources; they are not consumers.
    if (relative === CONTRACTS_PACKAGE || relative.startsWith(`${CONTRACTS_PACKAGE}/`)) continue;
    const content = fs.readFileSync(file, "utf8");
    const owner = owningPackage(rootDir, path.dirname(file));

    for (const { specifier, line } of extractSpecifiers(content)) {
      if (specifier.startsWith(".")) {
        if (!relative.startsWith("web/")) continue;
        const resolved = path.resolve(path.dirname(file), specifier);
        const fromContracts = path.relative(contractsDir, resolved);
        if (!fromContracts.startsWith("..") && !path.isAbsolute(fromContracts)) {
          errors.push({
            file,
            line,
            message: `Web code imports the generated package by relative path '${specifier}'; import '${PACKAGE_SPECIFIER}' or a declared exports subpath instead.`,
          });
        }
        continue;
      }
      if (!SPECIFIER_PATTERN.test(specifier)) continue;
      const key = subpathKey(specifier);
      if (key === null) continue;
      if (!entries.has(key)) {
        errors.push({
          file,
          line,
          message: `Specifier '${specifier}' is not a declared exports subpath of ${PACKAGE_SPECIFIER}.`,
        });
        continue;
      }
      consumers.get(key).push({ file: relative, line });
      const declared = declaredWorkspaceDependency(owner?.manifest);
      if (declared === undefined) {
        errors.push({
          file,
          line,
          message: `'${specifier}' is imported without declaring "${PACKAGE_SPECIFIER}": "workspace:*" in ${owner ? toPosix(path.relative(rootDir, owner.manifestPath)) : "a package manifest"}.`,
        });
      } else if (declared !== "workspace:*") {
        errors.push({
          file,
          line,
          message: `'${specifier}' requires the workspace protocol ("workspace:*"), found '${declared}'.`,
        });
      }
    }
  }

  // Rule 4: every export is consumed, or explicitly registered with a reason.
  for (const key of entries.keys()) {
    const used = consumers.get(key) ?? [];
    const registered = Object.hasOwn(unboundRegistry, key);
    if (used.length === 0 && !registered) {
      errors.push({
        file: manifestPath,
        line: 1,
        message: `Export '${key}' has no consumer. Bind it in web (or a workspace consumer), or register the reason in ${REGISTRY_PATH}.`,
      });
    } else if (used.length > 0 && registered) {
      errors.push({
        file: registryFile,
        line: 1,
        message: `Export '${key}' is registered as unbound but is consumed by ${used[0].file}:${used[0].line}; remove the stale registration.`,
      });
    }
  }

  return errors;
}

if (process.argv[1] && path.resolve(process.argv[1]) === __filename) {
  console.log("Checking generated-contract consumer bindings...");
  const errors = runValidation(repoRoot);

  for (const err of errors) {
    const relPath = toPosix(path.relative(repoRoot, err.file));
    console.error(`\x1b[31m[CONTRACT-CONSUMER-ERROR]\x1b[0m ${relPath}:${err.line}`);
    console.error(`  \x1b[1mRule Violation:\x1b[0m ${err.message}`);
    console.error("");
  }

  if (errors.length > 0) {
    console.error(
      `\x1b[31m[FAILED]\x1b[0m Contract consumer binding validation failed with ${errors.length} error(s).`,
    );
    process.exit(1);
  }
  console.log(
    "\x1b[32m[PASSED]\x1b[0m Generated-contract exports, consumers and workspace dependencies are aligned.",
  );
  process.exit(0);
}
