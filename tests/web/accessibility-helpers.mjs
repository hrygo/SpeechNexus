import AxeBuilder from "@axe-core/playwright";
import { expect } from "@playwright/test";

export const tags = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];

// Existing text over the reviewed gradient/glass shell needs manual raster
// review as well as the solid-surface semantic probes. New locations fail.
const gradientText = [
  [
    ".brand > span, #page-title, .hero-status strong, .capability-card h2, .boundary-note p",
    "--ink",
  ],
  [
    ".hero-description, .hero-status small, .card-label, .capability-card p, footer > span",
    "--muted",
  ],
  [".eyebrow, .airgap-badge", "--accent-text"],
  [".boundary-label", "--warning"],
  ...["ready", "contract", "planned"].map((state) => [
    `.card-state[data-state="${state}"]`,
    `--status-${state}-text`,
  ]),
];

export async function scan(page, info) {
  const result = await new AxeBuilder({ page }).withTags(tags).analyze();
  await info.attach("axe-results", {
    body: JSON.stringify(
      { url: page.url(), violations: result.violations, incomplete: result.incomplete },
      null,
      2,
    ),
    contentType: "application/json",
  });
  expect(
    result.violations.map((r) => ({
      id: r.id,
      nodes: r.nodes.map((n) => ({ target: n.target, failure: n.failureSummary })),
    })),
  ).toEqual([]);
  for (const rule of result.incomplete) {
    expect(rule.id, "Unexpected incomplete rule requires review").toBe("color-contrast");
    for (const node of rule.nodes) {
      const checks = [...node.any, ...node.all, ...node.none];
      expect(
        checks.every(
          (c) =>
            c.id === "color-contrast" && ["bgGradient", "bgImage"].includes(c.data?.messageKey),
        ),
      ).toBe(true);
      expect(node.target).toHaveLength(1);
      const matched = await page.locator(node.target[0]).evaluate((element, rules) => {
        const match = rules.find(([selector]) => element.matches(selector));
        if (!match) return false;
        const probe = document.createElement("span");
        probe.style.color = `var(${match[1]})`;
        element.append(probe);
        const equal = getComputedStyle(element).color === getComputedStyle(probe).color;
        probe.remove();
        return equal;
      }, gradientText);
      expect(matched, `Unreviewed gradient text or foreground drift: ${node.target[0]}`).toBe(true);
    }
  }
  return result;
}

export async function semanticContrast(page, info) {
  await page.evaluate(() => {
    const pairs = [];
    for (const fg of ["--ink", "--muted", "--muted-subtle", "--accent-text"]) {
      for (const bg of ["--page", "--surface", "--surface-strong", "--surface-glass"])
        pairs.push([fg, bg]);
    }
    for (const state of ["ready", "contract", "planned"])
      pairs.push([`--status-${state}-text`, `--status-${state}-bg`]);
    pairs.push(["--accent-text", "--accent-soft"], ["--warning", "--warning-soft"]);
    const container = document.createElement("div");
    container.id = "a11y-semantic-probes";
    container.style.backgroundColor = "var(--page)";
    for (const [fg, bg] of pairs) {
      const style = getComputedStyle(document.querySelector(".app-shell"));
      if (!style.getPropertyValue(fg).trim() || !style.getPropertyValue(bg).trim())
        throw new Error(`Missing semantic token: ${fg}/${bg}`);
      const probe = document.createElement("div");
      probe.textContent = `${fg} on ${bg}`;
      probe.style.cssText = `font-size:14px;font-weight:400;opacity:1;color:var(${fg});background-color:var(${bg});padding:4px;`;
      container.append(probe);
    }
    document.querySelector(".app-shell").append(container);
  });
  try {
    const result = await new AxeBuilder({ page })
      .include("#a11y-semantic-probes")
      .withRules(["color-contrast"])
      .analyze();
    if (info)
      await info.attach("semantic-contrast", {
        body: JSON.stringify(result, null, 2),
        contentType: "application/json",
      });
    expect(
      result.violations.map((r) =>
        r.nodes.map((n) => ({ target: n.target, failure: n.failureSummary })),
      ),
    ).toEqual([]);
    expect(result.incomplete, "Semantic contrast probes must produce determinate evidence").toEqual(
      [],
    );
    expect(result.passes.some((r) => r.id === "color-contrast")).toBe(true);
    expect(result.passes.find((r) => r.id === "color-contrast").nodes).toHaveLength(21);
  } finally {
    await page.locator("#a11y-semantic-probes").evaluate((node) => node.remove());
  }
}

export function contrastRatio(a, b) {
  const luminance = (rgb) =>
    rgb
      .map((v) => v / 255)
      .map((v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4))
      .reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
  const x = luminance(a),
    y = luminance(b);
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}

export async function keyboardAndFocus(page) {
  await page.keyboard.press("Tab");
  const skip = page.getByRole("link", { name: "跳到主要内容" });
  await expect(skip).toBeFocused();
  await expect(skip).toBeInViewport();
  await page.keyboard.press("Enter");
  const main = page.getByRole("main");
  await expect(main).toBeFocused({ timeout: 1000 });
  await expect(main).toBeInViewport();
  const ring = await main.evaluate((node) => {
    const style = getComputedStyle(node);
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 1;
    const ctx = canvas.getContext("2d");
    const pixel = (color) => {
      ctx.clearRect(0, 0, 1, 1);
      ctx.fillStyle = color;
      ctx.fillRect(0, 0, 1, 1);
      return [...ctx.getImageData(0, 0, 1, 1).data];
    };
    const bgProbe = document.createElement("span");
    node.append(bgProbe);
    const pairs = ["--page", "--surface", "--surface-strong"].map((token) => {
      bgProbe.style.backgroundColor = `var(${token})`;
      const bg = getComputedStyle(bgProbe).backgroundColor;
      const back = pixel(bg);
      ctx.fillStyle = style.outlineColor;
      ctx.fillRect(0, 0, 1, 1);
      return [back, [...ctx.getImageData(0, 0, 1, 1).data]];
    });
    bgProbe.remove();
    return { style: style.outlineStyle, width: parseFloat(style.outlineWidth), pairs };
  });
  expect(ring.style).not.toBe("none");
  expect(ring.width).toBeGreaterThanOrEqual(2);
  for (const [bg, fg] of ring.pairs)
    expect(contrastRatio(bg.slice(0, 3), fg.slice(0, 3))).toBeGreaterThanOrEqual(3);
}
