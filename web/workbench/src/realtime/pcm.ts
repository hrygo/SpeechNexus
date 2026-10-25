/** Public v1 PCM: 24 kHz mono signed 16-bit little-endian, at most 250 ms. */
export function encodePcm(samples: Float32Array): string {
  if (!samples.length || samples.length > 6000 || !samples.every(Number.isFinite))
    throw Error("Invalid realtime PCM");
  const bytes = new Uint8Array(samples.length * 2),
    view = new DataView(bytes.buffer);
  samples.forEach((value, i) => {
    const clamped = Math.max(-1, Math.min(1, value));
    view.setInt16(i * 2, Math.round(clamped * (clamped < 0 ? 32768 : 32767)), true);
  });
  return btoa(String.fromCharCode(...bytes));
}
