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
 * before installing project dependencies. It enforces:
 *
 * 1. Every declared `exports` target is an existing file. A generated artifact
 *    that moved without its `exports` entry fails here even though a relative
 *    path would still reach it.
 * 2. Code under `web/` must not import `packages/contracts/src/**` by relative
 *    path; imports go through `@speechnexus/contracts[/<subpath>]`. A dynamic
 *    `import()`/`require()` under `web/` must carry a literal specifier so the
 *    binding stays statically checkable.
 * 3. Every `@speechnexus/contracts[/<subpath>]` specifier (static, `import type`,
 *    side-effect or literal `require()`) maps to a declared export, and the
 *    importing workspace package declares `"@speechnexus/contracts": "workspace:*"`.
 * 4. Every declared subpath has at least one such consumer, or is registered in
 *    `packages/contracts/export-consumers.json` with verifiable evidence:
 *    either a `consumer.path` file that really imports the generated Python
 *    module derived from the subpath (`x-http` -> `x_http`), or an explicit
 *    `consumer: null` plus a `retention` entry whose binding profile is a
 *    `client: false` candidate, whose decision document exists, and whose
 *    enable/delete conditions are stated. A registration for a subpath that
 *    gains a consumer, a consumer path that disappears, a missing derived
 *    import, a retained candidate that gains a consumer, and a new export
 *    without any of these all fail.
 *
 * The guard is a static text scan: a manifest import counts as a consumer even
 * when the binding is unused or a side-effect import, and nothing here proves
 * the import executes at runtime. See `packages/contracts/README.md`.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, "../..");

export const CONTRACTS_PACKAGE = "packages/contracts";
export const PACKAGE_SPECIFIER = "@speechnexus/contracts";
export const REGISTRY_PATH = `${CONTRACTS_PACKAGE}/export-consumers.json`;
export const BINDINGS_PATH = "contracts/bindings.json";
export const SCAN_ROOTS = ["web", "scripts", "tests", "packages"];
export const CODE_EXTENSIONS = [".ts", ".tsx", ".mts", ".cts", ".mjs", ".js", ".jsx", ".cjs"];
export const PYTHON_EXTENSIONS = [".py"];
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
  ".mypy_cache",
  ".ruff_cache",
  ".pytest_cache",
]);

const SPECIFIER_PATTERN = /^@speechnexus\/contracts(?:\/|$)/;
const PYTHON_MODULE_PATTERN = /^[a-z0-9_]+$/;

// `/` starts a regular expression only where an expression may begin.
const REGEX_PREFIX_CHARS = new Set("([{,;:=!&|?+-*%<>~^/".split(""));
const REGEX_PREFIX_WORDS = new Set([
  "return",
  "typeof",
  "case",
  "in",
  "of",
  "new",
  "delete",
  "void",
  "throw",
  "do",
  "else",
  "yield",
  "await",
]);

// Only clause tokens may sit between the keyword and `from`: identifiers,
// `as`/`type`/namespace stars, separators. A single character class keeps the
// check linear; operators, semicolons or JSX tags reject the candidate.
const IMPORT_CLAUSE_ALLOWED = /^[\s,{}*.\w$]*$/;

function toPosix(value) {
  return value.split(path.sep).join("/");
}

function lineOf(content, index) {
  return content.slice(0, index).split("\n").length;
}

function realpathOr(value) {
  try {
    return fs.realpathSync(value);
  } catch {
    return value;
  }
}

/**
 * Resolve a relative specifier the way the bundler/tsc do: extensionless
 * imports and directory `index` files both count, and the result is a real
 * path so a symlink cannot hide access to the generated package.
 */
function resolveImportTarget(resolved) {
  if (fs.existsSync(resolved)) return realpathOr(resolved);
  for (const extension of CODE_EXTENSIONS) {
    const candidate = `${resolved}${extension}`;
    if (fs.existsSync(candidate)) return realpathOr(candidate);
  }
  for (const extension of CODE_EXTENSIONS) {
    const candidate = path.join(resolved, `index${extension}`);
    if (fs.existsSync(candidate)) return realpathOr(candidate);
  }
  return resolved;
}

/** List code files under `rootDir`, deduplicated by real path. */
export function listConsumerFiles(rootDir) {
  const files = [];
  const seenDirectories = new Set();
  const walk = (dir) => {
    const real = realpathOr(dir);
    if (seenDirectories.has(real)) return;
    seenDirectories.add(real);
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      if (entry.name.startsWith(".") || SKIP_DIRECTORIES.has(entry.name)) continue;
      const full = path.join(dir, entry.name);
      let stat;
      try {
        stat = fs.statSync(full);
      } catch {
        continue;
      }
      if (stat.isDirectory()) walk(full);
      else if (CODE_EXTENSIONS.includes(path.extname(entry.name))) files.push(full);
    }
  };
  for (const root of SCAN_ROOTS) walk(path.join(rootDir, root));
  return files.sort();
}

/** Blank comments, string bodies and template bodies while keeping positions. */
export function maskSource(code) {
  const literals = new Map();
  const chars = code.split("");
  const blank = (from, to) => {
    for (let index = from; index < to; index += 1) {
      if (chars[index] !== "\n") chars[index] = " ";
    }
  };
  let previous;
  let index = 0;
  while (index < code.length) {
    const char = code[index];
    if (char === "/" && code[index + 1] === "/") {
      const end = code.indexOf("\n", index);
      const stop = end < 0 ? code.length : end;
      blank(index, stop);
      index = stop;
      continue;
    }
    if (char === "/" && code[index + 1] === "*") {
      const end = code.indexOf("*/", index + 2);
      const stop = end < 0 ? code.length : end + 2;
      blank(index, stop);
      index = stop;
      continue;
    }
    if (char === "/" && regexAllowed(previous)) {
      let cursor = index + 1;
      let inClass = false;
      let closed = false;
      while (cursor < code.length) {
        const next = code[cursor];
        if (next === "\\") {
          cursor += 2;
          continue;
        }
        if (next === "\n") break;
        if (next === "[") inClass = true;
        else if (next === "]") inClass = false;
        else if (next === "/" && !inClass) {
          closed = true;
          break;
        }
        cursor += 1;
      }
      const stop = closed ? cursor + 1 : cursor;
      blank(index, stop);
      index = stop;
      previous = "regex";
      continue;
    }
    if (char === "'" || char === '"') {
      let cursor = index + 1;
      while (cursor < code.length) {
        if (code[cursor] === "\\") {
          cursor += 2;
          continue;
        }
        if (code[cursor] === char) break;
        if (code[cursor] === "\n") break;
        cursor += 1;
      }
      if (code[cursor] === char) {
        literals.set(index, { end: cursor + 1, value: code.slice(index + 1, cursor) });
        blank(index + 1, cursor);
        index = cursor + 1;
        previous = "string";
      } else {
        index += 1;
      }
      continue;
    }
    if (char === "`") {
      let cursor = index + 1;
      let depth = 0;
      while (cursor < code.length) {
        const next = code[cursor];
        if (next === "\\") {
          cursor += 2;
          continue;
        }
        if (next === "$" && code[cursor + 1] === "{") {
          depth += 1;
          cursor += 2;
          continue;
        }
        if (next === "}" && depth > 0) {
          depth -= 1;
          cursor += 1;
          continue;
        }
        if (next === "`" && depth === 0) break;
        cursor += 1;
      }
      if (code[cursor] === "`") {
        literals.set(index, { end: cursor + 1, value: code.slice(index + 1, cursor) });
        blank(index + 1, cursor);
        index = cursor + 1;
        previous = "string";
      } else {
        index += 1;
      }
      continue;
    }
    if (/\s/.test(char)) {
      index += 1;
      continue;
    }
    const word = /^[A-Za-z_$][\w$]*|^\d+(?:\.\d+)?/.exec(code.slice(index));
    if (word) {
      previous = word[0];
      index += word[0].length;
      continue;
    }
    previous = char;
    index += 1;
  }
  return { masked: chars.join(""), literals };
}

function regexAllowed(previous) {
  if (previous === undefined) return true;
  if (previous === "regex" || previous === "string") return false;
  if (previous.length === 1) return REGEX_PREFIX_CHARS.has(previous);
  return REGEX_PREFIX_WORDS.has(previous);
}

/**
 * True when `from` at `index` belongs to an `import`/`export` clause rather than
 * to ordinary prose or JSX text. The clause may never contain operators, tags
 * or statements between the keyword and `from`.
 */
function looksLikeImportClause(masked, keywordStarts, index) {
  for (let cursor = keywordStarts.length - 1; cursor >= 0; cursor -= 1) {
    const start = keywordStarts[cursor];
    if (start >= index) continue;
    const between = masked.slice(start, index);
    return IMPORT_CLAUSE_ALLOWED.test(between.replace(/^(?:import|export)/, ""));
  }
  return false;
}

/**
 * Extract import/export specifiers and statically unverifiable dynamic imports.
 * Comments, string bodies and template bodies never produce specifiers.
 */
export function scanSource(code) {
  const { masked, literals } = maskSource(code);
  const specifiers = [];
  const unverifiable = [];
  const addSpecifier = (index, kind) => {
    const literal = literals.get(index);
    if (literal === undefined) return;
    specifiers.push({ specifier: literal.value, line: lineOf(masked, index), kind });
  };

  const keywordStarts = [...masked.matchAll(/\b(?:import|export)\b/g)].map((match) => match.index);
  for (const match of masked.matchAll(/\bfrom\s+(["'])/g)) {
    const quoteIndex = match.index + match[0].length - 1;
    if (!looksLikeImportClause(masked, keywordStarts, match.index)) continue;
    addSpecifier(quoteIndex, "static");
  }
  for (const match of masked.matchAll(/(?:^|\n)[ \t]*import\s+(["'])/g)) {
    const quoteIndex = match.index + match[0].length - 1;
    addSpecifier(quoteIndex, "side-effect");
  }
  for (const match of masked.matchAll(/\b(import|require)\s*\(\s*/g)) {
    const form = match[1] === "import" ? "dynamic import" : "require";
    const position = match.index + match[0].length;
    const char = masked[position];
    if (char === '"' || char === "'") {
      addSpecifier(position, form);
    } else if (char === "`") {
      unverifiable.push({
        line: lineOf(masked, position),
        form: `${form} with a template-literal specifier`,
      });
    } else if (char !== undefined) {
      unverifiable.push({
        line: lineOf(masked, position),
        form: `${form} with a non-literal specifier`,
      });
    }
  }
  return { specifiers, unverifiable };
}

/** Read the package manifest exports as a map of subpath key to declared target. */
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

/** Resolve a string `exports` target, or null when the declaration is nested. */
export function exportTarget(value) {
  if (typeof value === "string") return value;
  if (value !== null && typeof value === "object") {
    const target = value.import ?? value.types ?? value.default;
    if (typeof target === "string") return target;
  }
  return null;
}

/** Map an import specifier to an exports subpath key (`.` for the root). */
export function subpathKey(specifier) {
  if (specifier === PACKAGE_SPECIFIER) return ".";
  if (!specifier.startsWith(`${PACKAGE_SPECIFIER}/`)) return null;
  return `./${specifier.slice(PACKAGE_SPECIFIER.length + 1)}`;
}

/** The generated Python module that mirrors one TypeScript subpath. */
export function pythonModuleFor(key) {
  if (!key.startsWith("./") || !PYTHON_MODULE_PATTERN.test(key.slice(2).replaceAll("-", "_"))) {
    return null;
  }
  return `speechnexus_contracts.${key.slice(2).replaceAll("-", "_")}`;
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
      if (typeof manifest.name === "string") return { dir, manifestPath, manifest };
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

function safeRelative(rootDir, file) {
  const relative = toPosix(path.relative(rootDir, file));
  return relative.startsWith("..") || path.isAbsolute(relative) ? null : relative;
}

/** Python files that import one generated module, outside the package itself. */
export function findPythonImporters(rootDir, module) {
  const importers = [];
  const pattern = new RegExp(
    `(?:^|\\n)\\s*(?:from\\s+${module.replaceAll(".", "\\.")}\\s+import\\b|import\\s+${module.replaceAll(".", "\\.")}\\b)`,
  );
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
      if (entry.isDirectory()) {
        const relative = safeRelative(rootDir, full);
        if (relative !== null && relative.startsWith(CONTRACTS_PACKAGE)) continue;
        walk(full);
      } else if (PYTHON_EXTENSIONS.includes(path.extname(entry.name))) {
        const relative = safeRelative(rootDir, full);
        if (relative === null || relative.startsWith(CONTRACTS_PACKAGE)) continue;
        if (pattern.test(fs.readFileSync(full, "utf8"))) importers.push(relative);
      }
    }
  };
  walk(rootDir);
  return importers.sort();
}

function checkConsumer(rootDir, key, consumer, errors, registryFile) {
  if (typeof consumer !== "object" || consumer === null || Array.isArray(consumer)) {
    errors.push({
      file: registryFile,
      line: 1,
      message: `Export '${key}' must declare a consumer object or "consumer": null with retention.`,
    });
    return;
  }
  if (typeof consumer.path !== "string" || consumer.path === "") {
    errors.push({
      file: registryFile,
      line: 1,
      message: `Export '${key}' consumer needs a repository-relative "path".`,
    });
    return;
  }
  const absolute = path.resolve(rootDir, consumer.path);
  const relative = safeRelative(rootDir, absolute);
  if (relative === null || relative.startsWith("..")) {
    errors.push({
      file: registryFile,
      line: 1,
      message: `Export '${key}' consumer path '${consumer.path}' escapes the repository.`,
    });
    return;
  }
  if (relative.startsWith(CONTRACTS_PACKAGE)) {
    errors.push({
      file: registryFile,
      line: 1,
      message: `Export '${key}' consumer path '${consumer.path}' is the generated package itself, not a consumer.`,
    });
    return;
  }
  if (!fs.existsSync(absolute) || !fs.statSync(absolute).isFile()) {
    errors.push({
      file: registryFile,
      line: 1,
      message: `Export '${key}' consumer path '${consumer.path}' does not exist.`,
    });
    return;
  }
  const module = pythonModuleFor(key);
  if (module === null) {
    errors.push({
      file: registryFile,
      line: 1,
      message: `Export '${key}' has no derivable generated Python module.`,
    });
    return;
  }
  if (consumer.module !== undefined && consumer.module !== module) {
    errors.push({
      file: registryFile,
      line: 1,
      message: `Export '${key}' consumer.module '${consumer.module}' must equal the derived '${module}'.`,
    });
    return;
  }
  if (path.extname(absolute) !== ".py") {
    errors.push({
      file: registryFile,
      line: 1,
      message: `Export '${key}' consumer '${consumer.path}' must be a Python file importing '${module}'.`,
    });
    return;
  }
  const pattern = new RegExp(
    `(?:^|\\n)\\s*(?:from\\s+${module.replaceAll(".", "\\.")}\\s+import\\b|import\\s+${module.replaceAll(".", "\\.")}\\b)`,
  );
  if (!pattern.test(fs.readFileSync(absolute, "utf8"))) {
    errors.push({
      file: registryFile,
      line: 1,
      message: `Export '${key}' consumer '${consumer.path}' does not import ${module}.`,
    });
  }
}

function checkRetention(rootDir, key, retention, errors, registryFile) {
  if (typeof retention !== "object" || retention === null || Array.isArray(retention)) {
    errors.push({
      file: registryFile,
      line: 1,
      message: `Export '${key}' with "consumer": null needs a retention object.`,
    });
    return;
  }
  for (const field of ["decision", "enable", "delete"]) {
    if (typeof retention[field] !== "string" || retention[field].trim() === "") {
      errors.push({
        file: registryFile,
        line: 1,
        message: `Export '${key}' retention needs a non-empty '${field}' condition.`,
      });
    }
  }
  if (typeof retention.basis !== "string" || retention.basis === "") {
    errors.push({
      file: registryFile,
      line: 1,
      message: `Export '${key}' retention needs a decision document in 'basis'.`,
    });
  } else if (!fs.existsSync(path.resolve(rootDir, retention.basis))) {
    errors.push({
      file: registryFile,
      line: 1,
      message: `Export '${key}' retention basis '${retention.basis}' does not exist.`,
    });
  }
  if (typeof retention.binding !== "string" || retention.binding === "") {
    errors.push({
      file: registryFile,
      line: 1,
      message: `Export '${key}' retention needs the bindings.json profile in 'binding'.`,
    });
    return;
  }
  const bindingsFile = path.join(rootDir, BINDINGS_PATH);
  const bindings = readJson(bindingsFile, errors, "contracts/bindings.json");
  const profile = bindings?.profiles?.[retention.binding];
  if (profile === undefined) {
    errors.push({
      file: registryFile,
      line: 1,
      message: `Export '${key}' retention binding '${retention.binding}' is not a contracts/bindings.json profile.`,
    });
  } else if (profile.client !== false) {
    errors.push({
      file: registryFile,
      line: 1,
      message: `Export '${key}' retention binding '${retention.binding}' generates a client (client != false); a consumerless export is not a retained candidate anymore.`,
    });
  }
}

export function runValidation(rootDir = repoRoot) {
  const errors = [];
  const contractsDir = path.join(rootDir, CONTRACTS_PACKAGE);
  const contractsReal = realpathOr(contractsDir);
  const manifestPath = path.join(contractsDir, "package.json");

  // Rule 1: every declared export points at an existing file.
  let entries;
  try {
    ({ entries } = readExports(rootDir));
  } catch (error) {
    return [{ file: manifestPath, line: 1, message: `${error.message}` }];
  }
  for (const [key, value] of entries) {
    const target = exportTarget(value);
    if (target === null) {
      errors.push({
        file: manifestPath,
        line: 1,
        message: `Export '${key}' has no string import/types target (nested condition objects are not supported).`,
      });
      continue;
    }
    const resolved = path.resolve(contractsDir, target);
    if (!resolved.startsWith(contractsDir + path.sep) || !fs.existsSync(resolved)) {
      errors.push({
        file: manifestPath,
        line: 1,
        message: `Export '${key}' targets a missing artifact '${target}'.`,
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
  if (registry !== null && typeof registry === "object" && !Array.isArray(registry)) {
    for (const [key, entry] of Object.entries(unboundRegistry)) {
      if (!entries.has(key)) {
        errors.push({
          file: registryFile,
          line: 1,
          message: `Registered export '${key}' is not declared in packages/contracts/package.json.`,
        });
        continue;
      }
      if (typeof entry !== "object" || entry === null || Array.isArray(entry)) {
        errors.push({
          file: registryFile,
          line: 1,
          message: `Registered export '${key}' must be an object with 'reason' and 'consumer'.`,
        });
        continue;
      }
      if (typeof entry.reason !== "string" || entry.reason.trim() === "") {
        errors.push({
          file: registryFile,
          line: 1,
          message: `Registered export '${key}' needs a non-empty 'reason'.`,
        });
      }
      if (entry.consumer === null) {
        checkRetention(rootDir, key, entry.retention, errors, registryFile);
        const module = pythonModuleFor(key);
        const pythonImporters = module === null ? [] : findPythonImporters(rootDir, module);
        if (pythonImporters.length > 0) {
          errors.push({
            file: registryFile,
            line: 1,
            message: `Export '${key}' is registered as consumerless but ${pythonImporters[0]} imports ${module}.`,
          });
        }
      } else {
        checkConsumer(rootDir, key, entry.consumer, errors, registryFile);
      }
    }
  }

  const consumers = new Map([...entries.keys()].map((key) => [key, []]));

  // Rules 2-3: manifest imports, declared dependency, no relative package access.
  for (const file of listConsumerFiles(rootDir)) {
    const relative = toPosix(path.relative(rootDir, file));
    if (relative === CONTRACTS_PACKAGE || relative.startsWith(`${CONTRACTS_PACKAGE}/`)) continue;
    const content = fs.readFileSync(file, "utf8");
    const { specifiers, unverifiable } = scanSource(content);
    const owner = owningPackage(rootDir, path.dirname(file));

    for (const { specifier, line } of specifiers) {
      if (specifier.startsWith(".")) {
        if (!relative.startsWith("web/")) continue;
        const real = resolveImportTarget(path.resolve(path.dirname(file), specifier));
        if (real === contractsReal || real.startsWith(contractsReal + path.sep)) {
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

    if (relative.startsWith("web/")) {
      for (const { line, form } of unverifiable) {
        errors.push({
          file,
          line,
          message: `Web code uses ${form}; the contract binding must be statically checkable with a literal specifier.`,
        });
      }
    }
  }

  // Rule 4: every export is consumed, or registered with verifiable evidence.
  for (const key of entries.keys()) {
    const used = consumers.get(key) ?? [];
    const registered = Object.hasOwn(unboundRegistry, key);
    if (used.length === 0 && !registered) {
      errors.push({
        file: manifestPath,
        line: 1,
        message: `Export '${key}' has no consumer. Bind it in web (or a workspace consumer), or register the reason and evidence in ${REGISTRY_PATH}.`,
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
