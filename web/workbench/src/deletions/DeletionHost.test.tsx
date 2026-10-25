import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { parse, WorkbenchClient } from "../manuscripts/workbench-client";
import { sample } from "../meetings/meeting-fixtures";
import DeletionHost from "./DeletionHost";

function setup() {
  const p = parse("DeletionPlan", sample("workbenchCreateDeletionPlan"));
  p.mode = "cascade";
  p.expires_at = "2099-01-01T00:00:00Z";
  p.dependencies = [
    {
      target: {
        owner: "workbench",
        kind: "transcript",
        resource: { id: "child-demo", revision: "2" },
      },
      relation: "owned_child",
      disposition: "cascade",
    },
    {
      target: {
        owner: "speech",
        kind: "artifact",
        resource: { id: "retained-demo", revision: "1" },
      },
      relation: "independent_copy",
      disposition: "retain",
    },
  ];
  const project = parse("Project", sample("getProject"));
  const client = new WorkbenchClient(vi.fn());
  vi.spyOn(client, "status").mockResolvedValue({
    ...parse("WorkbenchStatusV2", sample("getWorkbenchStatusV2")),
    workbench_enabled: true,
  });
  vi.spyOn(client, "projects").mockResolvedValue({
    object: "nexus.list",
    data: [project],
    has_more: false,
    next_cursor: null,
  });
  vi.spyOn(client, "project").mockResolvedValue(project);
  vi.spyOn(client, "transcripts").mockResolvedValue(
    parse("TranscriptList", sample("listTranscripts")),
  );
  const preview = vi.spyOn(client, "previewDeletion").mockResolvedValue(p);
  const accept = vi
    .spyOn(client, "acceptDeletion")
    .mockResolvedValue(parse("Deletion", sample("workbenchCreateDeletion")));
  vi.spyOn(client, "deletion").mockResolvedValue(parse("Deletion", sample("workbenchGetDeletion")));
  const data = new Map<string, string>();
  const storage = {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => {
      data.set(k, v);
    },
  };
  return { p, project, client, storage, preview, accept };
}
describe("reviewable deletion UI", () => {
  it("requires every exact target and shows retained dependencies separately from accepted and current facts", async () => {
    const f = setup();
    render(<DeletionHost client={f.client} storage={f.storage} />);
    fireEvent.click(await screen.findByRole("button", { name: f.project.name }));
    fireEvent.change(screen.getByLabelText("删除方式"), { target: { value: "cascade" } });
    fireEvent.click(await screen.findByRole("button", { name: "生成删除预检" }));
    const checks = await screen.findAllByRole("checkbox");
    expect(checks).toHaveLength(2);
    expect(screen.getByText(/retained-demo/)).toBeInTheDocument();
    const submit = screen.getByRole("button", { name: "确认所列范围并提交删除" });
    expect(submit).toBeDisabled();
    fireEvent.click(checks[0]!);
    expect(submit).toBeDisabled();
    fireEvent.click(checks[1]!);
    expect(submit).toBeEnabled();
    fireEvent.click(submit);
    await waitFor(() => expect(f.accept).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole("heading", { name: "当前清理状态" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "原始接受回执" })).toBeInTheDocument();
    expect(screen.queryByText("清理完成")).not.toBeInTheDocument();
  });
  it("never replaces a corrupt recovery record or displays raw service errors", () => {
    const f = setup(),
      setItem = vi.fn();
    render(<DeletionHost client={f.client} storage={{ getItem: () => "broken", setItem }} />);
    expect(screen.getByRole("alert")).toHaveTextContent("恢复信息不可读取");
    fireEvent.click(screen.getByRole("button", { name: "重新读取恢复信息" }));
    expect(setItem).not.toHaveBeenCalled();
    expect(f.preview).not.toHaveBeenCalled();
  });
});
