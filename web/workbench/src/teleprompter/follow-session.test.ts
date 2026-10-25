import { describe, expect, it } from "vitest";
import { FollowSession, restoreCheckpoint } from "./follow-session";

const script = (text = "Welcome everyone. Today we discuss careful listening.") => ({
  id: "local-script",
  revision: 1,
  text,
});

describe("fixed script following", () => {
  it("preserves original text and advances over a growing hypothesis", () => {
    const original = script("\uFEFFWelcome, everyone!\r\nToday we discuss careful listening. 👩🏽‍💻");
    const session = new FollowSession(original);
    const context = session.beginFollowing("epoch-one");
    expect(session.acceptHypothesis(context, { sequence: 1, text: "welcome everyone" })).toBe(
      "advanced",
    );
    expect(session.snapshot().position).toBe(Array.from("\uFEFFWelcome, everyone").length);
    expect(session.acceptHypothesis(context, { sequence: 2, text: "Welcome everyone today" })).toBe(
      "advanced",
    );
    expect(session.snapshot().script).toEqual(original);
    expect(session.snapshot().position).toBe(
      Array.from("\uFEFFWelcome, everyone!\r\nToday").length,
    );
  });

  it("does not move backwards on a revised preview or replay", () => {
    const session = new FollowSession(script());
    const context = session.beginFollowing("e1");
    session.acceptHypothesis(context, { sequence: 2, text: "Today we discuss" });
    const position = session.snapshot().position;
    expect(session.acceptHypothesis(context, { sequence: 1, text: "careful listening" })).toBe(
      "stale",
    );
    expect(session.acceptHypothesis(context, { sequence: 2, text: "careful listening" })).toBe(
      "stale",
    );
    expect(session.acceptHypothesis(context, { sequence: 3, text: "Welcome everyone" })).toBe(
      "held",
    );
    expect(session.snapshot().position).toBe(position);
  });

  it.each([
    ["go forward. go forward.", "go forward", "ambiguous"],
    ["A short statement.", "a", "insufficient"],
    ["A short statement.", "nothing matches", "unmatched"],
    ["corporate question", "rate", "unmatched"],
    ["开始朗读，然后继续。", "开始朗读", "advanced"],
  ])("conservatively matches %s", (text, hypothesis, decision) => {
    const session = new FollowSession(script(text));
    const context = session.beginFollowing("e1");
    expect(session.acceptHypothesis(context, { sequence: 1, text: hypothesis })).toBe(decision);
    if (decision !== "advanced") expect(session.snapshot().position).toBe(0);
  });

  it("does not jump to a unique phrase beyond its finite window", () => {
    const session = new FollowSession(
      script("Intro. " + "padding ".repeat(40) + "destination phrase"),
    );
    const context = session.beginFollowing("e1");
    expect(session.acceptHypothesis(context, { sequence: 1, text: "destination phrase" })).toBe(
      "unmatched",
    );
    expect(session.snapshot().position).toBe(0);
  });

  it("retires callbacks on manual takeover, pause, same-epoch restart and different epoch", () => {
    const session = new FollowSession(script());
    const first = session.beginFollowing("e1");
    session.acceptHypothesis(first, { sequence: 1, text: "Welcome everyone" });
    const restore = session.snapshot().position;
    session.seek(2);
    expect(session.snapshot().mode).toBe("manual");
    expect(session.acceptHypothesis(first, { sequence: 2, text: "careful listening" })).toBe(
      "stale",
    );
    session.restoreResumePoint();
    expect(session.snapshot().position).toBe(restore);
    const second = session.beginFollowing("e1");
    expect(session.acceptHypothesis(first, { sequence: 3, text: "careful listening" })).toBe(
      "stale",
    );
    session.pause();
    expect(session.acceptHypothesis(second, { sequence: 1, text: "careful listening" })).toBe(
      "stale",
    );
    const third = session.beginFollowing("e2");
    expect(session.acceptHypothesis(second, { sequence: 2, text: "careful listening" })).toBe(
      "stale",
    );
    expect(session.acceptHypothesis(third, { sequence: 1, text: "careful listening" })).toBe(
      "advanced",
    );
  });

  it("rejects wrong script identity/revision/epoch and foreign session context", () => {
    const session = new FollowSession(script());
    const context = session.beginFollowing("e1");
    const other = new FollowSession(script());
    const foreign = other.beginFollowing("e1");
    for (const wrong of [
      { ...context, scriptId: "different" },
      { ...context, scriptRevision: 2 },
      { ...context, streamEpoch: "e2" },
      foreign,
    ])
      expect(session.acceptHypothesis(wrong, { sequence: 1, text: "Welcome everyone" })).toBe(
        "stale",
      );
    expect(session.snapshot().position).toBe(0);
  });

  it("maps Unicode codepoint positions without splitting emoji and normalizes only match tokens", () => {
    const session = new FollowSession(script("😀 Ｈｅｌｌｏ world!"));
    const context = session.beginFollowing("e1");
    expect(session.acceptHypothesis(context, { sequence: 1, text: "hello world" })).toBe(
      "advanced",
    );
    expect(session.snapshot().position).toBe(Array.from("😀 Ｈｅｌｌｏ world").length);
    expect(session.snapshot().script.text).toBe("😀 Ｈｅｌｌｏ world!");
  });

  it.each([0, -1, 1.2, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1])(
    "rejects bad sequence %s",
    (sequence) => {
      const session = new FollowSession(script());
      const context = session.beginFollowing("e1");
      expect(() =>
        session.acceptHypothesis(context, { sequence, text: "Welcome everyone" }),
      ).toThrow();
      expect(session.snapshot().position).toBe(0);
    },
  );

  it("rejects oversized or malformed input without consuming a sequence", () => {
    const session = new FollowSession(script());
    const context = session.beginFollowing("e1");
    for (const text of ["x".repeat(257), "bad\0input", "bad\uD800input"])
      expect(() => session.acceptHypothesis(context, { sequence: 1, text })).toThrow();
    expect(session.acceptHypothesis(context, { sequence: 1, text: "Welcome everyone" })).toBe(
      "advanced",
    );
    for (const position of [-1, 1000, NaN, 1.5]) expect(() => session.seek(position)).toThrow();
  });

  it("keeps returned script/state immutable and rejects invalid script", () => {
    const input = script();
    const session = new FollowSession(input);
    input.text = "modified";
    expect(session.snapshot().script.text).not.toBe(input.text);
    expect(Object.isFrozen(session.snapshot())).toBe(true);
    expect(Object.isFrozen(session.snapshot().script)).toBe(true);
    for (const text of ["", "   ", "x".repeat(20001), "\uD800", "\0"])
      expect(() => new FollowSession(script(text))).toThrow();
    expect(() => new FollowSession({ ...script(), revision: 0 })).toThrow();
  });
});

describe("independent local checkpoint", () => {
  it("restores exact script, visible position and takeover point, always manual with no active callback", () => {
    const session = new FollowSession(script());
    const context = session.beginFollowing("e1");
    session.acceptHypothesis(context, { sequence: 1, text: "Welcome everyone" });
    const resume = session.snapshot().position;
    session.seek(2);
    const raw = session.checkpoint();
    expect(raw).not.toContain("streamEpoch");
    const restored = restoreCheckpoint(raw);
    expect(restored.snapshot()).toMatchObject({
      script: script(),
      position: 2,
      resumePosition: resume,
      mode: "manual",
      streamEpoch: null,
    });
    expect(restored.acceptHypothesis(context, { sequence: 10, text: "careful listening" })).toBe(
      "stale",
    );
    restored.restoreResumePoint();
    expect(restored.snapshot().position).toBe(resume);
  });

  it.each(["{", "null", "[]", "x".repeat(200001)])(
    "rejects malformed/bounded checkpoint",
    (raw) => {
      expect(() => restoreCheckpoint(raw)).toThrow();
    },
  );

  it("rejects unknown fields, invalid positions, unknown versions and forged script", () => {
    const base = JSON.parse(new FollowSession(script()).checkpoint()) as Record<string, unknown>;
    for (const change of [
      { version: 2 },
      { streamEpoch: "old" },
      { position: -1 },
      { position: 1000 },
      { resumePosition: 0.5 },
      { script: { ...script(), revision: true } },
      { script: { ...script(), extra: true } },
    ])
      expect(() => restoreCheckpoint(JSON.stringify({ ...base, ...change }))).toThrow();
  });
});
