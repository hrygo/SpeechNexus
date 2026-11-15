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
 * The guard is dependency-free Node plus the repository's Python interpreter, so
 * the `changes` job can run it before installing project dependencies. It
 * enforces:
 *
 * 1. Every declared `exports` target is an existing file. A generated artifact
 *    that moved without its `exports` entry fails here even though a relative
 *    path would still reach it.
 * 2. Code under `web/` must not import `packages/contracts/src/**` by relative
 *    path (resolved with the bundler's extension/index rules and canonicalized
 *    with `realpath`, so a symlink cannot hide it); imports go through
 *    `@speechnexus/contracts[/<subpath>]`, and a dynamic `import()`/`require()`
 *    must carry a literal specifier (an interpolation-free template literal is
 *    literal).
 * 3. Every `@speechnexus/contracts[/<subpath>]` specifier (static, `import type`,
 *    side-effect or literal `require()`) maps to a declared export, and the
 *    importing workspace package declares `"@speechnexus/contracts": "workspace:*"`.
 * 4. Every declared subpath is accounted for mechanically -- there is no
 *    free-text exemption:
 *      a. it has a manifest consumer; or
 *      b. its derived generated Python module
 *         (`packages/contracts/src/speechnexus_contracts/<module>.py`) exists and
 *         at least one repository `.py` file (outside the generated package)
 *         really imports it, decided by Python's own `tokenize` so comments,
 *         docstrings and string literals never count; or
 *      c. both of the above are false but the subpath is a generated candidate:
 *         its derived module file exists, and `contracts/bindings.json` has a
 *         profile whose normalized name equals the subpath, declares
 *         `"client": false`, and whose `source` file exists. This is the
 *         mechanical definition of "generated but not consumed yet"; no
 *         reason/basis/decision text can substitute for it.
 *    A subpath whose `exports` target is not a Python module (for example a
 *    `schemas/*.json` bundle) has no candidate channel and needs a consumer.
 *
 * The guard is a static text scan: a manifest import counts as a consumer even
 * when the binding is unused or a side-effect import, and nothing here proves
 * the import executes at runtime. See `packages/contracts/README.md`.
 */

import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, "../..");
const PYTHON_SCANNER = path.join(__dirname, "scan-contract-imports.py");

export const CONTRACTS_PACKAGE = "packages/contracts";
export const PACKAGE_SPECIFIER = "@speechnexus/contracts";
export const BINDINGS_PATH = "contracts/bindings.json";
export const GENERATED_PYTHON_DIR = path.join(CONTRACTS_PACKAGE, "src", "speechnexus_contracts");
export const SCAN_ROOTS = ["web", "scripts", "tests", "packages"];
export const CODE_EXTENSIONS = [".ts", ".tsx", ".mts", ".cts", ".mjs", ".js", ".jsx", ".cjs"];
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

const IDENTIFIER = /^[\p{ID_Start}$_][\p{ID_Continue}$\u200C\u200D]*/u;
const IDENTIFIER_PART = /^[\p{ID_Continue}$\u200C\u200D]$/u;

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

/**
 * List code files under `rootDir`. Symlinked directories are not entered (they
 * would duplicate the real tree and produce one error per generated file);
 * files are deduplicated by real path.
 */
export function listConsumerFiles(rootDir) {
  const files = [];
  const seenFiles = new Set();
  const walk = (dir) => {
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      if (entry.name.startsWith(".") || SKIP_DIRECTORIES.has(entry.name)) continue;
      if (entry.isSymbolicLink()) continue;
      const full = path.join(dir, entry.name);
      let stat;
      try {
        stat = fs.statSync(full);
      } catch {
        continue;
      }
      if (stat.isDirectory()) {
        walk(full);
      } else if (CODE_EXTENSIONS.includes(path.extname(entry.name))) {
        const real = realpathOr(full);
        if (seenFiles.has(real)) continue;
        seenFiles.add(real);
        files.push(full);
      }
    }
  };
  for (const root of SCAN_ROOTS) walk(path.join(rootDir, root));
  return files.sort();
}

/**
 * Blank comments, string bodies and template literal text while keeping code
 * positions. Template interpolations (`${...}`) stay code, so `import(...)`
 * inside an interpolation is still seen.
 */
export function maskSource(code) {
  const literals = new Map();
  const chars = code.split("");
  const blank = (from, to) => {
    for (let index = from; index < to; index += 1) {
      if (chars[index] !== "\n") chars[index] = " ";
    }
  };
  let previous;

  function scanTemplate(index) {
    const start = index;
    let cursor = start + 1;
    let interpolated = false;
    while (cursor < code.length) {
      const char = code[cursor];
      if (char === "\\") {
        cursor += 2;
        continue;
      }
      if (char === "$" && code[cursor + 1] === "{") {
        interpolated = true;
        blank(start + 1, cursor);
        previous = undefined;
        cursor = scanCode(cursor + 2, "}");
        if (cursor >= code.length) {
          previous = "string";
          return code.length;
        }
        cursor += 1;
        previous = undefined;
        continue;
      }
      if (char === "`") {
        blank(start + 1, cursor);
        literals.set(start, {
          end: cursor + 1,
          value: code.slice(start + 1, cursor),
          template: true,
          interpolated,
        });
        previous = "string";
        return cursor + 1;
      }
      cursor += 1;
    }
    previous = "string";
    return code.length;
  }

  function scanCode(index, terminator) {
    let depth = 0;
    while (index < code.length) {
      const char = code[index];
      if (terminator !== undefined) {
        if (char === "{" && terminator === "}") depth += 1;
        else if (char === "}") {
          if (depth === 0) return index;
          depth -= 1;
        }
      }
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
        index = scanTemplate(index);
        continue;
      }
      if (/\s/.test(char)) {
        index += 1;
        continue;
      }
      const word = IDENTIFIER.exec(code.slice(index)) ?? /^\d+(?:\.\d+)?/.exec(code.slice(index));
      if (word !== null) {
        previous = word[0];
        index += word[0].length;
        continue;
      }
      previous = char;
      index += 1;
    }
    return index;
  }

  scanCode(0, undefined);
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
 * to ordinary prose or JSX text. Only clause tokens may sit between the keyword
 * and `from`; operators, tags and statements reject the candidate.
 */
function looksLikeImportClause(masked, keywordStarts, index) {
  for (let cursor = keywordStarts.length - 1; cursor >= 0; cursor -= 1) {
    const start = keywordStarts[cursor];
    if (start >= index) continue;
    const between = masked.slice(start, index).replace(/^(?:import|export)/, "");
    for (const char of between) {
      if (/\s/.test(char)) continue;
      if ("{},*.$_".includes(char)) continue;
      if (IDENTIFIER_PART.test(char)) continue;
      return false;
    }
    return true;
  }
  return false;
}

/**
 * Extract import/export specifiers and statically unverifiable dynamic imports.
 * Comments, string bodies and template literal text never produce specifiers;
 * an interpolation-free template literal used as a dynamic import specifier is
 * static and does produce one.
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
      const literal = literals.get(position);
      if (literal !== undefined && literal.template && !literal.interpolated) {
        specifiers.push({
          specifier: literal.value,
          line: lineOf(masked, position),
          kind: `${form} (template literal)`,
        });
      } else {
        unverifiable.push({
          line: lineOf(masked, position),
          form: `${form} with an interpolated template-literal specifier`,
        });
      }
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

/** Normalize a subpath or profile name for the mechanical candidate pairing. */
function normalizedName(value) {
  return value.toLowerCase().replaceAll("-", "_");
}

/**
 * Report which generated Python modules are imported by repository `.py` files.
 * Python's own tokenizer decides, so comments, docstrings and ordinary string
 * literals never count as consumers.
 */
export function scanPythonImports(rootDir) {
  const result = spawnSync("python3", [PYTHON_SCANNER, "--root", rootDir], {
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
  if (result.error !== undefined) {
    return {
      error: `python3 with the standard library is required to verify Python import evidence: ${result.error.message}`,
    };
  }
  if (result.status !== 0) {
    return {
      error: `Python import scan failed: ${(result.stderr || result.stdout || "").trim()}`,
    };
  }
  let parsed;
  try {
    parsed = JSON.parse(result.stdout);
  } catch (error) {
    return { error: `Python import scan returned invalid JSON: ${error.message}` };
  }
  const imports = new Map();
  for (const [module, occurrences] of Object.entries(parsed.imports ?? {})) {
    imports.set(module, occurrences);
  }
  return { imports, unparsed: parsed.unparsed ?? [] };
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

/** Find the `client: false` bindings profile that generates this subpath. */
function candidateProfile(rootDir, key, errors, seen) {
  if (seen.bindings === undefined) {
    seen.bindings = readJson(path.join(rootDir, BINDINGS_PATH), errors, BINDINGS_PATH);
  }
  const bindings = seen.bindings;
  if (bindings === null || typeof bindings !== "object") return null;
  const wanted = normalizedName(key.slice(2));
  for (const [name, profile] of Object.entries(bindings.profiles ?? {})) {
    if (normalizedName(name) !== wanted) continue;
    if (profile?.client !== false) continue;
    if (typeof profile.source !== "string") continue;
    const source = path.resolve(rootDir, "contracts", profile.source);
    if (!fs.existsSync(source) || !fs.statSync(source).isFile()) continue;
    return { name, source: profile.source };
  }
  return null;
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

  const consumers = new Map([...entries.keys()].map((key) => [key, []]));

  // Rules 2-3: manifest imports, declared dependency, no relative package access.
  for (const file of listConsumerFiles(rootDir)) {
    const relative = toPosix(path.relative(rootDir, file));
    if (relative === CONTRACTS_PACKAGE || relative.startsWith(`${CONTRACTS_PACKAGE}/`)) continue;
    const realFile = realpathOr(file);
    if (
      relative.startsWith("web/") &&
      (realFile === contractsReal || realFile.startsWith(contractsReal + path.sep))
    ) {
      errors.push({
        file,
        line: 1,
        message: `Web file is a symlink into the generated package; import '${PACKAGE_SPECIFIER}' or a declared exports subpath instead.`,
      });
      continue;
    }
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
          message: `Web code uses ${form}; the contract binding must be statically checkable (use a string literal or an interpolation-free template literal).`,
        });
      }
    }
  }

  // Rule 4: every export has a mechanical account -- consumer, real Python
  // import, or a generated client-less candidate. No free-text exemption exists.
  const python = scanPythonImports(rootDir);
  if (python.error !== undefined) {
    errors.push({ file: PYTHON_SCANNER, line: 1, message: python.error });
  } else {
    for (const relative of python.unparsed) {
      errors.push({
        file: path.join(rootDir, relative),
        line: 1,
        message: `Cannot tokenize '${relative}' while verifying Python import evidence.`,
      });
    }
  }
  const seen = {};
  for (const key of entries.keys()) {
    const used = consumers.get(key) ?? [];
    if (used.length > 0) continue;
    const module = pythonModuleFor(key);
    if (module === null) {
      errors.push({
        file: manifestPath,
        line: 1,
        message: `Export '${key}' has no consumer. Non-Python subpath bundles (for example schemas/*.json) have no candidate channel; add a manifest consumer.`,
      });
      continue;
    }
    const moduleFile = path.join(rootDir, GENERATED_PYTHON_DIR, `${module.split(".").pop()}.py`);
    const moduleExists = fs.existsSync(moduleFile) && fs.statSync(moduleFile).isFile();
    if (python.error === undefined) {
      const importers = python.imports.get(module) ?? [];
      if (importers.length > 0) {
        if (moduleExists) continue;
        errors.push({
          file: manifestPath,
          line: 1,
          message: `Export '${key}' is imported by ${importers[0].file} but the generated module '${toPosix(path.relative(rootDir, moduleFile))}' is missing.`,
        });
        continue;
      }
    }
    if (!moduleExists) {
      errors.push({
        file: manifestPath,
        line: 1,
        message: `Export '${key}' has no consumer and no generated module '${toPosix(path.relative(rootDir, moduleFile))}'; it cannot be a generated candidate.`,
      });
      continue;
    }
    const profile = candidateProfile(rootDir, key, errors, seen);
    if (profile === null) {
      errors.push({
        file: manifestPath,
        line: 1,
        message: `Export '${key}' has no consumer and no matching "client": false profile in ${BINDINGS_PATH} (profile name normalized to '${normalizedName(key.slice(2))}' with an existing source).`,
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
    "\x1b[32m[PASSED]\x1b[0m Generated-contract exports, consumers and workspace bindings are mechanically accounted for.",
  );
  process.exit(0);
}
