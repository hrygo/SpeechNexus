// Runs inside Chromium; all DOM references remain in its JSHandle.
export async function collectRegions(selectors) {
  await document.fonts.ready;
  if (devicePixelRatio !== 1) throw new Error("Raster contrast: DPR must be 1");
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 1;
  const ctx = canvas.getContext("2d");
  const lines = [],
    seen = new Set();
  const records = [],
    paint = new Map();
  const glass = [...document.querySelectorAll("*")].filter(
    (node) => getComputedStyle(node).backdropFilter !== "none",
  );
  const properties = [
    "backgroundColor",
    "backgroundImage",
    "backdropFilter",
    "color",
    "webkitTextFillColor",
    "opacity",
    "filter",
    "mixBlendMode",
    "transform",
    "fontSize",
    "lineHeight",
    "letterSpacing",
  ];
  for (const selector of selectors) {
    const elements = document.querySelectorAll(selector);
    if (elements.length !== 1) throw new Error(`Raster contrast: ambiguous target ${selector}`);
    const element = elements[0];
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    let text;
    let found = 0;
    while ((text = walker.nextNode())) {
      if (!text.textContent.trim()) continue;
      found++;
      // Axe can report both a container and its inline descendants. A target
      // still has text when another target already supplied its evidence.
      if (seen.has(text)) continue;
      seen.add(text);
      const parent = text.parentElement;
      const style = getComputedStyle(parent);
      let panel, influence;
      for (let node = parent; node; node = node.parentElement) {
        const s = getComputedStyle(node);
        paint.set(
          node,
          properties.map((p) => s[p]),
        );
        if (s.backdropFilter !== "none") {
          if (panel) throw new Error(`Raster contrast: unsupported nested glass at ${selector}`);
          panel = node;
          const r = node.getBoundingClientRect();
          const blur =
            Math.max(
              0,
              ...[...s.backdropFilter.matchAll(/blur\(([\d.]+)px\)/g)].map((m) => Number(m[1])),
            ) * 4;
          influence = {
            left: r.left + scrollX - blur,
            right: r.right + scrollX + blur,
            top: r.top + scrollY - blur,
            bottom: r.bottom + scrollY + blur,
          };
        }
        const generated = ["::before", "::after"].some(
          (p) => !["none", "normal", '""', "''"].includes(getComputedStyle(node, p).content),
        );
        if (
          Number(s.opacity) !== 1 ||
          s.filter !== "none" ||
          s.mixBlendMode !== "normal" ||
          s.transform !== "none" ||
          s.maskImage !== "none" ||
          s.clipPath !== "none" ||
          s.position === "fixed" ||
          s.position === "sticky" ||
          s.backgroundImage.includes("url(") ||
          node.getAnimations().some((a) => a.playState === "running") ||
          generated
        ) {
          throw new Error(`Raster contrast: unsupported paint effect at ${selector}`);
        }
        for (const pseudo of ["::first-line", "::first-letter"]) {
          const p = getComputedStyle(node, pseudo);
          ctx.clearRect(0, 0, 1, 1);
          ctx.fillStyle = p.backgroundColor;
          ctx.fillRect(0, 0, 1, 1);
          const paintsBackground = ctx.getImageData(0, 0, 1, 1).data[3] !== 0;
          if (
            paintsBackground ||
            p.backgroundImage !== "none" ||
            p.filter !== "none" ||
            p.maskImage !== "none" ||
            p.clipPath !== "none" ||
            p.opacity !== "1" ||
            p.mixBlendMode !== "normal" ||
            p.boxShadow !== "none" ||
            [
              "color",
              "webkitTextFillColor",
              "fontSize",
              "fontWeight",
              "fontStyle",
              "textShadow",
              "webkitTextStrokeWidth",
            ].some((key) => p[key] !== s[key])
          )
            throw new Error(`Raster contrast: unsupported pseudo text at ${selector}`);
        }
      }
      if (
        style.textShadow !== "none" ||
        style.textDecorationLine !== "none" ||
        parseFloat(style.webkitTextStrokeWidth) > 0
      )
        throw new Error(`Raster contrast: unsupported text effect at ${selector}`);
      ctx.clearRect(0, 0, 1, 1);
      ctx.fillStyle = style.webkitTextFillColor;
      ctx.fillRect(0, 0, 1, 1);
      const fg = [...ctx.getImageData(0, 0, 1, 1).data];
      const color = style.webkitTextFillColor;
      const alpha = color.match(/(?:\/|,)\s*([\d.e+-]+)(%)?\s*\)$/);
      const explicitAlpha = color.startsWith("rgba(") || color.includes("/");
      if (
        fg[3] !== 255 ||
        (explicitAlpha && alpha && Number(alpha[1]) / (alpha[2] ? 100 : 1) !== 1)
      )
        throw new Error(`Raster contrast: unsupported translucent text at ${selector}`);
      const fontSize = parseFloat(style.fontSize),
        weight = Number(style.fontWeight);
      const threshold = fontSize >= 24 || (fontSize >= 56 / 3 && weight >= 700) ? 3 : 4.5;
      const range = document.createRange();
      range.selectNodeContents(text);
      const rects = [...range.getClientRects()].filter((r) => r.width > 0 && r.height > 0);
      if (!rects.length) throw new Error(`Raster contrast: no rendered text at ${selector}`);
      for (const layer of glass) {
        if (layer.contains(text)) continue;
        const box = layer.getBoundingClientRect();
        if (
          rects.some(
            (r) =>
              r.left < box.right && r.right > box.left && r.top < box.bottom && r.bottom > box.top,
          )
        )
          throw new Error(`Raster contrast: unsupported sibling glass at ${selector}`);
      }
      const key = records.length;
      records.push({
        text,
        panel,
        influence,
        rects: rects.map((r) => ({
          left: r.left + scrollX,
          right: r.right + scrollX,
          top: r.top + scrollY,
          bottom: r.bottom + scrollY,
        })),
      });
      for (const r of rects)
        lines.push({
          key,
          target: selector,
          fg: fg.slice(0, 3),
          threshold,
          left: r.left + scrollX,
          top: r.top + scrollY,
          right: r.right + scrollX,
          bottom: r.bottom + scrollY,
        });
    }
    if (!found) throw new Error(`Raster contrast: no text at ${selector}`);
  }
  if (!lines.length || lines.length > 512)
    throw new Error("Raster contrast: invalid text region budget");
  const intersects = (a, b) =>
    a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
  const conflicts = records.map(() => []);
  for (let i = 0; i < records.length; i++)
    for (let j = i + 1; j < records.length; j++) {
      const a = records[i],
        b = records[j];
      if (a.rects.some((x) => b.rects.some((y) => intersects(x, y))))
        throw new Error("Raster contrast: unsupported overlapping text");
      if (
        (a.panel && !a.panel.contains(b.text) && b.rects.some((r) => intersects(r, a.influence))) ||
        (b.panel && !b.panel.contains(a.text) && a.rects.some((r) => intersects(r, b.influence)))
      ) {
        conflicts[i].push(j);
        conflicts[j].push(i);
      }
    }
  return { lines, records, paint, properties, conflicts, wrappers: [] };
}
