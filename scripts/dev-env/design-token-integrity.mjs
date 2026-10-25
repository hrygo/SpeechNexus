// Dependency-free checks for the project's CSS-string token format.
// This is deliberately not a DTCG parser or a browser cascade evaluator.
export function withoutComments(content, { script = false } = {}) {
  let result = "";
  let quote = "";
  for (let i = 0; i < content.length; i++) {
    const c = content[i];
    if (quote) {
      result += c;
      if (c === "\\") result += content[++i] ?? "";
      else if (c === quote) quote = "";
    } else if (c === '"' || c === "'" || (script && c === "`")) {
      quote = c;
      result += c;
    } else {
      const end = content.startsWith("/*", i)
        ? "*/"
        : content.startsWith("<!--", i)
          ? "-->"
          : script && content.startsWith("//", i)
            ? "\n"
            : null;
      if (end) {
        let stop = content.indexOf(end, i + 2);
        stop = stop < 0 ? content.length : stop + (end === "\n" ? 0 : end.length);
        result += content.slice(i, stop).replace(/[^\n]/g, " ");
        i = stop - 1;
      } else result += c;
    }
  }
  return result;
}

export function references(content) {
  return new Set([...content.matchAll(/\bvar\(\s*(--[\w-]+)\s*(?=[,)])/g)].map((m) => m[1]));
}

const maskStrings = (css) =>
  css.replace(/"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'/g, (s) => s.replace(/[^\n]/g, " "));

function cssDeclarations(content) {
  const css = withoutComments(content);
  return [...maskStrings(css).matchAll(/(?:^|[;{])\s*([\w-]+)\s*:\s*([^;{}]+)/g)].map((m) => {
    m[2] = css.slice(m.index + m[0].length - m[2].length, m.index + m[0].length);
    return m;
  });
}

export function declarations(content) {
  return cssDeclarations(content).filter((m) => m[1].startsWith("--"));
}

export function aliasCycles(content) {
  const cycles = [];
  // Only declarations in the same rule are combined. Theme overrides in other
  // selectors must not be merged into a fictitious cascade graph.
  const css = withoutComments(content);
  for (const block of maskStrings(css).matchAll(/\{([^{}]*)\}/g)) {
    const source = css.slice(block.index + 1, block.index + block[0].length - 1);
    const graph = new Map(declarations(`{${source}}`).map((m) => [m[1], references(m[2])]));
    const done = new Set();
    function visit(name, stack) {
      if (stack.includes(name)) {
        cycles.push([...stack, name].join(" → "));
        return;
      }
      if (done.has(name)) return;
      done.add(name);
      for (const ref of graph.get(name) ?? []) visit(ref, [...stack, name]);
    }
    for (const name of graph.keys()) visit(name, []);
  }
  return cycles;
}

// Named colors are checked only in color-bearing declarations/attributes;
// arbitrary prose, identifiers and CSS selectors are not color values.
const NAMED_COLORS = new Set(
  "aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue blueviolet brown burlywood cadetblue chartreuse chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan darkgoldenrod darkgray darkgreen darkgrey darkkhaki darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon darkseagreen darkslateblue darkslategray darkslategrey darkturquoise darkviolet deeppink deepskyblue dimgray dimgrey dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro ghostwhite gold goldenrod gray green greenyellow grey honeydew hotpink indianred indigo ivory khaki lavender lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan lightgoldenrodyellow lightgray lightgreen lightgrey lightpink lightsalmon lightseagreen lightskyblue lightslategray lightslategrey lightsteelblue lightyellow lime limegreen linen magenta maroon mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen mediumslateblue mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream mistyrose moccasin navajowhite navy oldlace olive olivedrab orange orangered orchid palegoldenrod palegreen paleturquoise palevioletred papayawhip peachpuff peru pink plum powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown seagreen seashell sienna silver skyblue slateblue slategray slategrey snow springgreen steelblue tan teal thistle tomato turquoise violet wheat white whitesmoke yellow yellowgreen".split(
    " ",
  ),
);

export function hardcodedColors(content, { css = false, palette = false } = {}) {
  let values = content;
  if (css) {
    values = cssDeclarations(content)
      .filter((m) => !(palette && m[1].startsWith("--")))
      .filter((m) => m[1] !== "content")
      .map((m) => `${m[1]}: ${m[2]}`)
      .join("\n");
  }
  values = values
    .replace(/\b(?:href|xlinkHref)\s*=\s*(?:\{\s*)?["'`]#[^"'`]*["'`]/g, "")
    .replace(/url\(\s*["']?#[^)]*\)/g, "");
  const literal =
    /#(?:[\da-f]{8}|[\da-f]{6}|[\da-f]{4}|[\da-f]{3})\b|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\s*\(/gi;
  const found = [...values.matchAll(literal)].map((m) => m[0]);
  for (const m of values.matchAll(
    /\b(?:[\w-]*[cC]olor|background|border[\w-]*|fill|stroke|outline|box-shadow|boxShadow|text-shadow|textShadow)["']?\s*[:=]\s*(?:\{\s*)?["'`]?([^;\n"'`<>}]+)/g,
  )) {
    // var names and URL fragments are not named colors.
    const value = m[1].replace(/url\([^)]*\)|--[\w-]+/g, "");
    for (const word of value.match(/\b[a-z]+\b/gi) ?? []) {
      if (NAMED_COLORS.has(word.toLowerCase())) found.push(word);
    }
  }
  return found;
}

const normalize = (value) =>
  value
    .trim()
    .replace(/\s+/g, " ")
    .replace(/\d*\.\d+/g, (n) => String(Number(n)));

export function validateRegistry(json, css) {
  const registered = new Set();
  const errors = [];
  const defined = new Map(declarations(css).map((m) => [m[1], m[2].trim()]));
  let registry;
  try {
    registry = JSON.parse(json);
  } catch {
    return { registered, errors: ["Invalid JSON token registry."] };
  }
  let count = 0;
  const types = new Set(["color", "dimension", "fontFamily", "number", "shadow"]);
  function visit(node, parts = []) {
    if (!node || typeof node !== "object" || Array.isArray(node)) return;
    if ("$type" in node && !("$value" in node)) {
      errors.push(`Invalid registry token '${parts.join(".")}': missing $value.`);
    }
    if ("$value" in node) {
      count++;
      if (!types.has(node.$type) || typeof node.$value !== "string" || !node.$value.trim()) {
        errors.push(
          `Invalid registry token '${parts.join(".")}': requires string $type and nonempty CSS-string $value.`,
        );
        return;
      }
      if (/[{}]/.test(node.$value) || "$ref" in node || "$extends" in node) {
        errors.push(`Unsupported alias in '${parts.join(".")}'; this registry uses CSS strings.`);
      }
      const primitive = parts[0] === "color" && parts[1] === "primitive";
      if (!primitive && !/^var\(\s*--[\w-]+\s*\)$/.test(node.$value)) {
        errors.push(
          `Unsupported registry value '${parts.join(".")}': use a single CSS var() reference.`,
        );
      }
      for (const ref of references(node.$value)) registered.add(ref);
      if (primitive) {
        const name = `--nexus-color-${parts.slice(2).join("-")}`;
        registered.add(name);
        if (!defined.has(name) || normalize(defined.get(name)) !== normalize(node.$value)) {
          errors.push(`Primitive registry/CSS mismatch (drift): '${name}'.`);
        }
      }
      return;
    }
    if ("$ref" in node || "$extends" in node)
      errors.push(`Unsupported alias in registry '${parts.join(".")}'.`);
    for (const [key, child] of Object.entries(node)) {
      if (!key.startsWith("$")) visit(child, [...parts, key]);
    }
  }
  visit(registry);
  if (!count) errors.push("Token registry contains no token values.");
  return { registered, errors };
}
