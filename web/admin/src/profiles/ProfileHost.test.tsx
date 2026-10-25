import { StrictMode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ProfileHost } from "./ProfileHost";
import { ProfileSession } from "./profile-session";
import { ProfileJournal } from "./profile-journal";
import { ProfileHttpError } from "./profile-client";
import { draft, request, storage } from "./profile-test-fixtures";

function setup() {
  const s = storage();
  const api = {
    create: vi.fn().mockResolvedValue(draft()),
    read: vi.fn().mockResolvedValue(draft()),
  };
  const session = new ProfileSession(api, new ProfileJournal(s), () => "original-profile-key");
  return { s, api, session };
}
function fill() {
  const { profile_id, expected_latest_revision, ...definition } = request();
  expect(expected_latest_revision).toBeNull();
  fireEvent.change(screen.getByLabelText("预设系列编号"), { target: { value: profile_id } });
  fireEvent.change(screen.getByLabelText("完整预设配置（JSON）"), {
    target: { value: JSON.stringify(definition) },
  });
  fireEvent.click(screen.getByRole("button", { name: "核对草稿" }));
}

it.each(["configuration_changed", "idempotency_conflict"])(
  "restores the durable %s refusal after reload without resending",
  async (code) => {
    const { api, session, s } = setup();
    session.prepare(request());
    api.create.mockRejectedValueOnce(new ProfileHttpError(409, code));
    await session.confirm();
    const restored = new ProfileSession(api, new ProfileJournal(s));
    render(<ProfileHost session={restored} />);
    expect(screen.getByRole("alert")).toHaveTextContent(
      code === "configuration_changed" ? "分配修订已改变" : "原键与服务端记录冲突",
    );
    expect(screen.getByLabelText("完整预设配置（JSON）")).toBeDisabled();
    expect(screen.getByRole("button", { name: "返回编辑" })).toBeEnabled();
    expect(api.create).toHaveBeenCalledTimes(1);
    expect(restored.snapshot().record).toMatchObject({ phase: "rejected", code, body: request() });
  },
);

it("mounts without IO, shows full intent confirmation, then displays only a draft receipt", async () => {
  const { api, session } = setup();
  render(
    <StrictMode>
      <ProfileHost session={session} />
    </StrictMode>,
  );
  expect(api.read).not.toHaveBeenCalled();
  expect(api.create).not.toHaveBeenCalled();
  expect(screen.getByText(/草稿不会发布或切换当前预设/)).toBeVisible();
  fill();
  expect(api.create).not.toHaveBeenCalled();
  const button = screen.getByRole("button", { name: "确认提交草稿" });
  expect(button).toHaveFocus();
  expect(screen.getByLabelText("待确认的完整草稿请求")).toHaveTextContent(
    '"expected_latest_revision": null',
  );
  fireEvent.click(button);
  expect(await screen.findByText(/本次服务端接收的草稿回执/)).toBeVisible();
  expect(screen.getByLabelText("草稿回执")).toHaveTextContent("draft");
  expect(api.create).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "返回编辑" }));
  expect(screen.getByLabelText("已知的最新分配修订")).toHaveValue("1");
});

it("keeps an invalid configuration in the editor without a network write", () => {
  const { api, session } = setup();
  render(<ProfileHost session={session} />);
  fireEvent.change(screen.getByLabelText("预设系列编号"), { target: { value: "caption-draft" } });
  fireEvent.change(screen.getByLabelText("完整预设配置（JSON）"), { target: { value: "{bad" } });
  fireEvent.click(screen.getByRole("button", { name: "核对草稿" }));
  expect(screen.getByRole("alert")).toHaveTextContent("配置未通过校验");
  expect(screen.getByLabelText("完整预设配置（JSON）")).toHaveValue("{bad");
  expect(api.create).not.toHaveBeenCalled();
});

it("restores unknown writes without resending and requires original-key replay after a fixed read", async () => {
  const { api, session, s } = setup();
  session.prepare(request());
  api.create.mockRejectedValueOnce(new ProfileHttpError(503, "profile_result_unknown"));
  await session.confirm();
  const restored = new ProfileSession(api, new ProfileJournal(s), () => "never-replace-the-key");
  render(<ProfileHost session={restored} />);
  expect(api.create).toHaveBeenCalledTimes(1);
  expect(screen.getByLabelText("预设系列编号")).toBeDisabled();
  expect(screen.getByRole("button", { name: "返回编辑" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("读取预设系列编号"), {
    target: { value: request().profile_id },
  });
  fireEvent.change(screen.getByLabelText("读取固定修订"), { target: { value: "1" } });
  fireEvent.click(screen.getByRole("button", { name: "读取固定修订" }));
  await screen.findByText(/本次固定读取/);
  expect(restored.snapshot().record?.phase).toBe("unknown");
  fireEvent.click(screen.getByRole("button", { name: "按原键重放草稿" }));
  await screen.findByText(/本次服务端接收的草稿回执/);
  expect(api.create.mock.calls[0]!.slice(0, 2)).toEqual(api.create.mock.calls[1]!.slice(0, 2));
});

it("presents CAS conflicts as refusal, preserves the definition and never silently rebases", async () => {
  const { api, session } = setup();
  api.create.mockRejectedValueOnce(new ProfileHttpError(409, "configuration_changed"));
  render(<ProfileHost session={session} />);
  fill();
  fireEvent.click(screen.getByRole("button", { name: "确认提交草稿" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("分配修订已改变"));
  expect(screen.getByLabelText("完整预设配置（JSON）")).toBeDisabled();
  expect(api.create).toHaveBeenCalledTimes(1);
  expect(screen.getByLabelText<HTMLTextAreaElement>("完整预设配置（JSON）").value).toContain(
    "Caption draft",
  );
});

it("copies a fixed revision only after an explicit action and strips publication metadata", async () => {
  const { api, session } = setup();
  render(<ProfileHost session={session} />);
  fireEvent.change(screen.getByLabelText("读取预设系列编号"), {
    target: { value: request().profile_id },
  });
  fireEvent.change(screen.getByLabelText("读取固定修订"), { target: { value: "1" } });
  fireEvent.click(screen.getByRole("button", { name: "读取固定修订" }));
  await screen.findByText(/本次固定读取/);
  expect(screen.getByLabelText("完整预设配置（JSON）")).toHaveValue("");
  fireEvent.click(screen.getByRole("button", { name: "以此修订为基础编辑" }));
  expect(screen.getByLabelText("已知的最新分配修订")).toHaveValue("1");
  const body: unknown = JSON.parse(
    screen.getByLabelText<HTMLTextAreaElement>("完整预设配置（JSON）").value,
  );
  expect(body).not.toHaveProperty("publication");
  expect(body).not.toHaveProperty("validation");
  expect(body).toMatchObject({ parameters: request().parameters });
  expect(api.create).not.toHaveBeenCalled();
});
