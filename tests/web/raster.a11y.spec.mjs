import { test, expect } from "@playwright/test";
import { scan } from "./accessibility-helpers.mjs";
import { rasterContrast } from "./raster-contrast.mjs";

test.beforeEach(async ({ context }) => {
  await context.route("**/*", (route) => {
    const url = new URL(route.request().url());
    return url.hostname === "127.0.0.1" && ["4173", "4174"].includes(url.port)
      ? route.continue()
      : route.abort("blockedbyclient");
  });
});

async function scene(page, css, html = '<p id="text">MMMMMMMMMMMMMMMMMMMM</p>') {
  await page.setContent(`<!doctype html><html lang="en"><head><title>Raster regression</title><style>
  body {margin:20px; background:linear-gradient(90deg,#fff,#eee);color:#111;font:16px Arial;}
  ${css}</style></head><body><main>${html}</main></body></html>`);
}

test("unchanged foreground token cannot hide a bad gradient", async ({ page }, info) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("http://127.0.0.1:4173");
  const before = await page.locator("h1").evaluate((node) => getComputedStyle(node).color);
  await page.addStyleTag({
    content: "body { background: linear-gradient(90deg, #eee, #fff) !important; }",
  });
  expect(await page.locator("h1").evaluate((node) => getComputedStyle(node).color)).toBe(before);
  await expect(scan(page, info)).rejects.toThrow(/Raster contrast/);
});

test("one CSS pixel bad stripe is not missed by sparse sampling", async ({ page }, info) => {
  await scene(page, "");
  expect((await rasterContrast(page, ["#text"], info)).length).toBeGreaterThan(0);
  await page.addStyleTag({
    content:
      "body {background:linear-gradient(90deg,#fff 0px 99px,#111 99px 100px,#fff 100px) fixed;}",
  });
  await expect(rasterContrast(page, ["#text"], info)).rejects.toThrow(/Raster contrast/);
});

test("glass alpha and backdrop filter use composited pixels", async ({ page }, info) => {
  await scene(
    page,
    "body {background:linear-gradient(90deg,#111,#222)} #text {color:#fff;background:rgba(255,255,255,.1);backdrop-filter:blur(12px) saturate(1.3);padding:20px;}",
  );
  const pass = await rasterContrast(page, ["#text"], info);
  expect(Math.min(...pass.map((r) => r.minimum))).toBeGreaterThan(4.5);
  await page.addStyleTag({ content: "#text {background:rgba(255,255,255,.95)}" });
  await expect(rasterContrast(page, ["#text"], info)).rejects.toThrow(/Raster contrast/);
});

test("hiding ink preserves currentColor background and restores it", async ({ page }, info) => {
  await scene(
    page,
    "#text {color:#111; background:currentColor;-webkit-text-fill-color:#fff;padding:10px;}",
  );
  const before = await page.locator("#text").evaluate((n) => ({
    color: getComputedStyle(n).color,
    fill: getComputedStyle(n).webkitTextFillColor,
    rect: n.getBoundingClientRect().toJSON(),
  }));
  await rasterContrast(page, ["#text"], info);
  const after = await page.locator("#text").evaluate((n) => ({
    color: getComputedStyle(n).color,
    fill: getComputedStyle(n).webkitTextFillColor,
    rect: n.getBoundingClientRect().toJSON(),
  }));
  expect(after).toEqual(before);
});

test("wrapped below-fold text is fully measured and scroll restored", async ({ page }, info) => {
  await scene(
    page,
    "#text {margin-top:1100px;width:200px;} body {background-attachment:fixed;}",
    `<p id="text">${"Readable text ".repeat(100)}</p>`,
  );
  await page.evaluate(() => scrollTo(0, 120));
  const result = await rasterContrast(page, ["#text"], info);
  expect(result.length).toBeGreaterThan(20);
  expect(new Set(result.map((r) => r.viewport.y)).size).toBeGreaterThan(1);
  expect(result.every((r) => r.samples > 0 && r.minimum >= 4.5)).toBe(true);
  expect(await page.evaluate(() => scrollY)).toBe(120);
});

for (const css of [
  "main {opacity:.5}",
  "main {filter:invert(1)}",
  "main {mix-blend-mode:multiply}",
  "#text::before {content:'generated'}",
  "#text {-webkit-text-fill-color:rgba(0,0,0,.5)}",
  "#text::first-line {color:white;-webkit-text-fill-color:white}",
  "#text::first-letter {color:white;-webkit-text-fill-color:white}",
  "#text::first-letter {background:#111}",
  "#text {mask-image:linear-gradient(rgba(0,0,0,.01),rgba(0,0,0,.01))}",
  "#text {clip-path:inset(0 99% 0 0)}",
  "#text {-webkit-text-fill-color:rgba(0,0,0,.99)}",
]) {
  test(`unsupported paint must fail closed: ${css}`, async ({ page }, info) => {
    await scene(page, css);
    await expect(rasterContrast(page, ["#text"], info)).rejects.toThrow(/unsupported/);
  });
}

test("nested axe targets measure each text node once in either order", async ({ page }, info) => {
  await scene(page, "", '<p id="text">Readable <code id="nested">nested text</code></p>');
  for (const targets of [
    ["#text", "#nested"],
    ["#nested", "#text"],
  ]) {
    const result = await rasterContrast(page, targets, info);
    expect(result).toHaveLength(2);
    await page.addStyleTag({ content: "#nested {color:#aaa}" });
    await expect(rasterContrast(page, targets, info)).rejects.toThrow(/has .*requires/);
    await page.addStyleTag({ content: "#nested {color:#111}" });
  }
});

test("short text incomplete results require passing raster evidence", async ({ page }, info) => {
  await scene(page, "", '<h1>Raster regression</h1><p id="text">无</p>');
  const result = await scan(page, info);
  expect(
    result.incomplete.some((r) =>
      r.nodes.some((n) =>
        [...n.any, ...n.all, ...n.none].some((c) => c.data?.messageKey === "shortTextContent"),
      ),
    ),
  ).toBe(true);
  expect(result.raster.some((r) => r.target === "#text" && r.samples > 0)).toBe(true);
  await page.addStyleTag({ content: "#text {color:#aaa}" });
  await expect(scan(page, info)).rejects.toThrow(/Raster contrast: .*has .*requires/);
});

test("no text cannot be treated as contrast evidence", async ({ page }, info) => {
  await scene(page, "", '<p id="text"></p>');
  await expect(rasterContrast(page, ["#text"], info)).rejects.toThrow(/no text/);
});

for (const nested of [true, false]) {
  test(`complex glass cannot change rejection into a joint pass: nested=${nested}`, async ({
    page,
  }, info) => {
    const b = '<span id="b">MM</span>',
      a = '<span id="a">MMM</span>';
    const html = nested
      ? `${b}<div id="outer"><div id="inner">${a}</div></div>`
      : `${b}<div id="outer"></div><div id="inner">${a}</div>`;
    await scene(
      page,
      `body {margin:0;background:white;} #b {position:absolute;left:110px;top:110px;font:40px Arial;color:#111} #outer {position:absolute;left:100px;top:100px;width:500px;height:200px;backdrop-filter:blur(30px)} #inner {position:absolute;left:${nested ? 100 : 200}px;top:${nested ? 20 : 120}px;width:200px;height:100px;backdrop-filter:blur(0px)} #a {color:#767676}`,
      html,
    );
    await expect(rasterContrast(page, ["#a", "#b"], info)).rejects.toThrow(/unsupported/);
  });
}

test("failed measurement restores original text and removes measurement wrappers", async ({
  page,
}, info) => {
  await scene(page, "#text {color:white}");
  const before = await page.locator("main").innerHTML();
  await expect(rasterContrast(page, ["#text"], info)).rejects.toThrow(/Raster contrast/);
  expect(await page.locator("main").innerHTML()).toBe(before);
  await page.addStyleTag({ content: "nexus-raster-ink {font-size:100px !important}" });
  await expect(rasterContrast(page, ["#text"], info)).rejects.toThrow(/geometry/);
  expect(await page.locator("main").innerHTML()).toBe(before);
});

test("text region and screenshot budgets fail rather than truncating evidence", async ({
  page,
}, info) => {
  await scene(
    page,
    "#text {width:10px;word-break:break-all;}",
    `<p id="text">${"M".repeat(600)}</p>`,
  );
  await expect(rasterContrast(page, ["#text"], info)).rejects.toThrow(/region budget/);
  await scene(
    page,
    "#text p {height:20px;margin-top:800px;}",
    `<div id="text">${"<p>Budget text</p>".repeat(13)}</div>`,
  );
  await expect(rasterContrast(page, ["#text"], info)).rejects.toThrow(/viewport budget/);
  expect(await page.locator("nexus-raster-ink").count()).toBe(0);
});
