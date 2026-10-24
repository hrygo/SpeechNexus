import { Buffer } from "node:buffer";

// Fixture recipes only. This module never enters a browser or production package.
export function exampleValue(example) {
  const value = structuredClone(example.value);
  if (Object.hasOwn(example, "synthetic_pcm_bytes")) {
    const count = example.synthetic_pcm_bytes;
    if (!Number.isInteger(count) || count < 1 || count > 8192) {
      throw new Error("Synthetic PCM recipes must be bounded");
    }
    if (!["input_audio_buffer.append", "nexus.tts.audio.delta"].includes(value.type)) {
      throw new Error("Synthetic PCM recipes require an audio event");
    }
    value.audio = Buffer.alloc(count).toString("base64");
  }
  return value;
}
