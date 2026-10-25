import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import {
  tags,
  scan,
  semanticContrast,
  keyboardAndFocus,
  contrastRatio,
} from "./accessibility-helpers.mjs";

const apps = [
  { name: "admin", port: 4173 },
  { name: "workbench", port: 4174 },
];

test.beforeEach(async ({ context }) => {
  await context.route("**/*", (route) => {
    const url = new URL(route.request().url());
    if (url.hostname === "127.0.0.1" && ["4173", "4174"].includes(url.port))
      return route.continue();
    return route.abort("blockedbyclient");
  });
});

for (const app of apps) {
  for (const theme of [
    { name: "system-light", system: "light" },
    { name: "system-dark", system: "dark" },
    { name: "explicit-light", system: "dark", explicit: "light" },
    { name: "explicit-dark", system: "light", explicit: "dark" },
  ]) {
    for (const width of [320, 1280]) {
      test(`${app.name} ${theme.name} ${width}: rendered WCAG, token contrast and keyboard`, async ({
        page,
      }, info) => {
        await page.setViewportSize({ width, height: 900 });
        await page.emulateMedia({ colorScheme: theme.system });
        await page.goto(`http://127.0.0.1:${app.port}`);
        if (theme.explicit)
          await page
            .locator("html")
            .evaluate((node, value) => (node.dataset.theme = value), theme.explicit);
        await expect(page.locator("html")).toHaveCSS(
          "color-scheme",
          theme.explicit ?? theme.system,
        );
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        const motion = await page.locator(".status-dot").evaluate((node) => {
          const s = getComputedStyle(node);
          return parseFloat(s.animationDuration) * Number(s.animationIterationCount);
        });
        expect(motion).toBeLessThanOrEqual(5);
        await scan(page, info);
        await semanticContrast(page, info);
        await keyboardAndFocus(page);
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
          width,
        );
        if (width === 1280 && theme.name.startsWith("system-")) {
          const path = info.outputPath("page.png");
          await page.screenshot({ path, fullPage: true });
          await info.attach("rendered-page", { path, contentType: "image/png" });
        }
      });
    }
  }
  for (const theme of ["light", "dark"]) {
    for (const { name, ...media } of [
      { name: "more contrast", contrast: "more" },
      { name: "forced colors", forcedColors: "active" },
    ]) {
      test(`${app.name} ${theme}: ${name} and reduced motion`, async ({ page }, info) => {
        await page.emulateMedia({ colorScheme: theme, ...media, reducedMotion: "reduce" });
        await page.goto(`http://127.0.0.1:${app.port}`);
        await expect(page.locator("html")).toHaveCSS(
          "color-scheme",
          media.forcedColors === "active" ? "light dark" : theme,
        );
        await scan(page, info);
        await semanticContrast(page, info);
        const motion = await page.locator(".status-dot").evaluate((node) => {
          const s = getComputedStyle(node);
          return {
            duration: parseFloat(s.animationDuration),
            iterations: s.animationIterationCount,
          };
        });
        expect(motion.duration).toBeLessThanOrEqual(0.01);
        expect(motion.iterations).not.toBe("infinite");
        await keyboardAndFocus(page);
      });
    }
  }
}

test("scanner detects missing names and actual low-contrast text", async ({ page }) => {
  await page.setContent(
    '<!doctype html><html lang="en"><head><title>Guard regression</title></head><body><main><button></button><p style="color:#aaa;background:#fff">Low contrast sentinel</p></main></body></html>',
  );
  const results = await new AxeBuilder({ page }).withTags(tags).analyze();
  expect(results.violations.some((r) => r.id === "button-name")).toBe(true);
  expect(results.violations.some((r) => r.id === "color-contrast")).toBe(true);
});

test("contrast math uses WCAG luminance and unrounded ratios", () => {
  expect(contrastRatio([0, 0, 0], [255, 255, 255])).toBe(21);
  expect(contrastRatio([255, 255, 255], [255, 255, 255])).toBe(1);
  expect(contrastRatio([119, 119, 119], [255, 255, 255])).toBeLessThan(4.5);
  expect(contrastRatio([118, 118, 118], [255, 255, 255])).toBeGreaterThan(4.5);
});

test("keyboard guard detects a broken skip target", async ({ page }) => {
  await page.goto("http://127.0.0.1:4173");
  await page.locator("main").evaluate((node) => node.removeAttribute("tabindex"));
  await expect(keyboardAndFocus(page)).rejects.toThrow();
});

test("keyboard guard detects an invisible focus ring", async ({ page }) => {
  await page.goto("http://127.0.0.1:4173");
  await page.addStyleTag({ content: ":focus-visible { outline: none !important; }" });
  await expect(keyboardAndFocus(page)).rejects.toThrow();
});

test("new indeterminate gradient text requires explicit review", async ({ page }, info) => {
  await page.goto("http://127.0.0.1:4173");
  await page.locator("main").evaluate((node) => {
    const text = document.createElement("p");
    text.id = "unreviewed-gradient";
    text.textContent = "New gradient text";
    node.append(text);
  });
  await expect(scan(page, info)).rejects.toThrow(/Unreviewed gradient/);
});
