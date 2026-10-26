import { describe, expect, it, vi } from "vitest";
import {
  ProfileClient,
  ProfileHttpError,
  profileRequest,
  PROFILE_JSON_BYTES,
  PROFILE_RESPONSE_BYTES,
} from "./profile-client";
import { draft, failure, request } from "./profile-test-fixtures";

describe("the same-source profile draft client", () => {
  it("keeps request and response byte limits bounded independently", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      new Response(" ".repeat(PROFILE_RESPONSE_BYTES) + JSON.stringify(draft()), {
        headers: { "Content-Type": "application/json" },
      }),
    );
    const c = new ProfileClient("https://deployment.invalid", fetch);
    const oversized = request({
      override_allowlist: ["asr.language"],
      override_ranges: { "asr.language": { enum: ["x".repeat(PROFILE_JSON_BYTES)] } },
    });
    await expect(c.create(oversized, "original-profile-key")).rejects.toThrow();
    expect(fetch).not.toHaveBeenCalled();
    await expect(c.read(request().profile_id, "1")).rejects.toThrow();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("accepts a near-limit complete request whose valid receipt includes server metadata", async () => {
    const base = request({
      override_allowlist: ["asr.language"],
      override_ranges: { "asr.language": { enum: ["x"] } },
    });
    const size = new TextEncoder().encode(JSON.stringify(base)).byteLength;
    const body = request({
      ...base,
      override_ranges: { "asr.language": { enum: ["x".repeat(262100 - size + 1)] } },
    });
    expect(new TextEncoder().encode(JSON.stringify(body)).byteLength).toBe(262100);
    const receipt = draft(body);
    expect(new TextEncoder().encode(JSON.stringify(receipt)).byteLength).toBeGreaterThan(
      PROFILE_JSON_BYTES,
    );
    const fetch = vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValue(Response.json(receipt, { status: 201 }));
    await expect(
      new ProfileClient("https://deployment.invalid", fetch).create(body, "original-profile-key"),
    ).resolves.toEqual(receipt);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("rejects an optional definition field injected into a receipt when absent in the intent", async () => {
    const body = request();
    delete body.description;
    const receipt = { ...draft(body), description: "server-injected" };
    const fetch = vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValue(Response.json(receipt, { status: 201 }));
    await expect(
      new ProfileClient("https://deployment.invalid", fetch).create(body, "original-profile-key"),
    ).rejects.toThrow();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("posts one exact explicit intent with a stable key and no default injection", async () => {
    const body = request({ expected_latest_revision: "9999999999999999998" });
    const fetch = vi.fn<typeof globalThis.fetch>().mockImplementation(async (input) => {
      const r = new Request(input);
      expect(r.url).toBe("https://deployment.invalid/v1/nexus/admin/profiles");
      expect(r.method).toBe("POST");
      expect(r.headers.get("idempotency-key")).toBe("original-profile-key");
      expect(r.redirect).toBe("error");
      expect(r.cache).toBe("no-store");
      expect(r.signal.aborted).toBe(false);
      expect(await r.json()).toEqual(body);
      return Response.json(draft(body), { status: 201 });
    });
    const c = new ProfileClient("https://deployment.invalid", fetch);
    expect((await c.create(body, "original-profile-key")).revision).toBe("9999999999999999999");
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("validates input before IO and requires explicit CAS without overwriting reserved fields", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>();
    const c = new ProfileClient("https://deployment.invalid", fetch);
    await expect(c.read("../foreign", "1")).rejects.toThrow();
    await expect(c.create(request(), "short")).rejects.toThrow();
    const { profile_id, expected_latest_revision, ...definition } = request();
    expect(expected_latest_revision).toBeNull();
    expect(profileRequest(profile_id, null, JSON.stringify(definition))).toEqual(request());
    expect(() => profileRequest(profile_id, "", JSON.stringify(definition))).toThrow();
    expect(() => profileRequest(profile_id, null, JSON.stringify(request()))).toThrow();
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each(["profile", "revision", "definition", "publication"])(
    "rejects a schema-valid but unrelated create %s instead of claiming success",
    async (change) => {
      const value = draft();
      if (change === "profile") value.profile_id = "foreign-profile";
      if (change === "revision") value.revision = "2";
      if (change === "definition") value.display_name = "different intent";
      if (change === "publication") {
        value.publication.status = "retired";
        value.publication.published_at = "2026-10-25T00:00:00Z";
        value.publication.retired_at = "2026-10-25T01:00:00Z";
      }
      const fetch = vi
        .fn<typeof globalThis.fetch>()
        .mockResolvedValue(Response.json(value, { status: 201 }));
      await expect(
        new ProfileClient("https://deployment.invalid", fetch).create(
          request(),
          "original-profile-key",
        ),
      ).rejects.toThrow();
      expect(fetch).toHaveBeenCalledTimes(1);
    },
  );

  it("keeps a fixed read distinct from a mutation result and rejects a mismatched revision", async () => {
    const value = draft();
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(Response.json(value));
    const c = new ProfileClient("https://deployment.invalid", fetch);
    await expect(c.read(value.profile_id, "1")).resolves.toEqual(value);
    await expect(c.read(value.profile_id, "2")).rejects.toThrow();
    expect(new Request(fetch.mock.calls[0]![0]).method).toBe("GET");
  });

  it("exposes only stable errors and never retries unknown results or follows redirects", async () => {
    const fetch = vi
      .fn<typeof globalThis.fetch>()
      .mockImplementation(() => Promise.resolve(failure()));
    const c = new ProfileClient("https://deployment.invalid", fetch);
    await expect(c.create(request(), "original-profile-key")).rejects.toEqual(
      new ProfileHttpError(503, "profile_result_unknown"),
    );
    expect(fetch).toHaveBeenCalledTimes(1);
    fetch.mockResolvedValueOnce(
      new Response(null, { status: 302, headers: { Location: "https://foreign.invalid" } }),
    );
    await expect(c.read(request().profile_id, "1")).rejects.toThrow();
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});
