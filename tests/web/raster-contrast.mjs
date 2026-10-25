import { collectRegions } from "./raster-regions.mjs";
import { contrastRatio } from "./contrast-math.mjs";

export async function rasterContrast(page, targets, info) {
  if (!targets.length) return [];
  const original = await page.evaluate(() => ({ x: scrollX, y: scrollY }));
  const reports = [];
  let state;
  try {
    state = await page.evaluateHandle(collectRegions, targets);
    const { lines, conflicts } = await state.evaluate((s) => ({
      lines: s.lines,
      conflicts: s.conflicts,
    }));

    const pending = new Set(lines.map((_, i) => i));
    for (let frame = 0; pending.size; frame++) {
      if (frame >= 12) throw new Error("Raster contrast: viewport budget exceeded");
      const first = lines[[...pending][0]];
      const viewport = await page.evaluate(async ({ top }) => {
        scrollTo({ left: 0, top: Math.max(0, top - innerHeight / 3), behavior: "instant" });
        await new Promise(requestAnimationFrame);
        return { x: scrollX, y: scrollY, width: innerWidth, height: innerHeight };
      }, first);
      let visible = [...pending].filter((i) => {
        const r = lines[i];
        return (
          r.left >= 0 &&
          r.right <= viewport.width &&
          r.top >= viewport.y &&
          r.bottom <= viewport.y + viewport.height
        );
      });
      if (!visible.length)
        throw new Error("Raster contrast: text is clipped or cannot fit in viewport");
      const chosen = [];
      for (const i of visible) {
        const key = lines[i].key;
        if (!chosen.some((k) => conflicts[key].includes(k)) && !chosen.includes(key))
          chosen.push(key);
      }
      visible = visible.filter((i) => chosen.includes(lines[i].key));
      await state.evaluate((s, keys) => {
        for (const key of keys) {
          const record = s.records[key];
          const wrapper = document.createElement("nexus-raster-ink");
          wrapper.style.cssText = "display:inline !important;opacity:0 !important";
          record.text.replaceWith(wrapper);
          wrapper.append(record.text);
          s.wrappers.push(wrapper);
          const range = document.createRange();
          range.selectNodeContents(record.text);
          const actual = [...range.getClientRects()];
          if (
            actual.length !== record.rects.length ||
            actual.some((r, i) =>
              ["left", "right", "top", "bottom"].some(
                (p) =>
                  Math.abs(
                    r[p] + (p === "left" || p === "right" ? scrollX : scrollY) - record.rects[i][p],
                  ) > 0.1,
              ),
            )
          )
            throw new Error("Raster contrast: hiding ink changed text geometry");
        }
        for (const [node, expected] of s.paint) {
          const style = getComputedStyle(node);
          if (s.properties.some((p, i) => style[p] !== expected[i]))
            throw new Error("Raster contrast: hiding ink changed paint styles");
        }
      }, chosen);
      let screenshot;
      try {
        screenshot = await page.screenshot({ scale: "css" });
      } finally {
        await state.evaluate((s) => {
          for (const w of s.wrappers) w.replaceWith(...w.childNodes);
          s.wrappers = [];
        });
      }
      const pixels = await page.evaluate(
        async ({ png, viewport, regions }) => {
          const image = new Image();
          image.src = `data:image/png;base64,${png}`;
          await image.decode();
          if (image.width !== viewport.width || image.height !== viewport.height)
            throw new Error("Raster contrast: screenshot dimensions changed");
          const canvas = document.createElement("canvas");
          canvas.width = image.width;
          canvas.height = image.height;
          const ctx = canvas.getContext("2d");
          ctx.drawImage(image, 0, 0);
          const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
          return regions.map((r) => {
            // Return unique background colors, not an unbounded RGBA image over RPC.
            const colors = new Set();
            let samples = 0;
            for (
              let y = Math.floor(r.top - viewport.y);
              y < Math.ceil(r.bottom - viewport.y);
              y++
            ) {
              for (let x = Math.floor(r.left); x < Math.ceil(r.right); x++) {
                const offset = (y * canvas.width + x) * 4;
                if (data[offset + 3] !== 255)
                  throw new Error("Raster contrast: nonopaque screenshot");
                colors.add((data[offset] << 16) | (data[offset + 1] << 8) | data[offset + 2]);
                samples++;
              }
            }
            return { colors: [...colors], samples };
          });
        },
        { png: screenshot.toString("base64"), viewport, regions: visible.map((i) => lines[i]) },
      );
      for (let j = 0; j < visible.length; j++) {
        const i = visible[j],
          r = lines[i],
          measured = pixels[j];
        let minimum = Infinity,
          worst;
        for (const encoded of measured.colors) {
          const bg = [encoded >> 16, (encoded >> 8) & 255, encoded & 255];
          const ratio = contrastRatio(r.fg, bg);
          if (ratio < minimum) {
            minimum = ratio;
            worst = bg;
          }
        }
        if (!measured.samples || !Number.isFinite(minimum))
          throw new Error("Raster contrast: empty evidence");
        reports.push({ ...r, minimum, worst, samples: measured.samples, viewport, frame });
        pending.delete(i);
      }
      if (reports.some((r) => r.minimum < r.threshold)) {
        if (info)
          await info.attach("raster-background-failure", {
            body: screenshot,
            contentType: "image/png",
          });
        const failed = reports.find((r) => r.minimum < r.threshold);
        throw new Error(
          `Raster contrast: ${failed.target} has ${failed.minimum.toFixed(3)}:1, requires ${failed.threshold}:1`,
        );
      }
    }
    return reports;
  } finally {
    if (state) {
      await state.evaluate((s) => {
        for (const w of s.wrappers) w.replaceWith(...w.childNodes);
      });
      await state.dispose();
    }
    await page.evaluate(async ({ x, y }) => {
      scrollTo({ left: x, top: y, behavior: "instant" });
      await new Promise(requestAnimationFrame);
    }, original);
    if (info)
      await info.attach("raster-contrast", {
        body: JSON.stringify(
          { method: "DPR1 sRGB8 line-box worst pixel", regions: reports },
          null,
          2,
        ),
        contentType: "application/json",
      });
  }
}
