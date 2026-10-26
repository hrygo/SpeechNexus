import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import InputDevicePicker from "./InputDevicePicker";

afterEach(() => vi.unstubAllGlobals());
function fixture() {
  const enumerateDevices = vi.fn().mockResolvedValue([
    { kind: "audioinput", deviceId: "private-one", label: "<b>Microphone</b>" },
    { kind: "videoinput", deviceId: "camera", label: "Camera" },
    { kind: "audioinput", deviceId: "private-two", label: "" },
    { kind: "audioinput", deviceId: "", label: "" },
  ]);
  const media = Object.assign(new EventTarget(), { enumerateDevices, getUserMedia: vi.fn() });
  vi.stubGlobal("navigator", { mediaDevices: media });
  const onSelect = vi.fn();
  return { media, enumerateDevices, onSelect };
}
describe("explicit local input device selection", () => {
  it("only enumerates on a gesture, filters inputs and never acquires media", async () => {
    const f = fixture();
    render(<InputDevicePicker value={null} onSelect={f.onSelect} disabled={false} />);
    expect(f.enumerateDevices).not.toHaveBeenCalled();
    expect(f.media.getUserMedia).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "读取麦克风列表" }));
    expect(await screen.findByRole("option", { name: "<b>Microphone</b>" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Camera" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("option")).toHaveLength(3);
    expect(
      screen.getByRole("option", { name: "麦克风 2（浏览器未提供名称）" }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("输入麦克风"), { target: { value: "private-two" } });
    expect(f.onSelect).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "应用麦克风选择" }));
    expect(f.onSelect).toHaveBeenCalledWith("private-two");
    expect(f.media.getUserMedia).not.toHaveBeenCalled();
  });
  it("retains missing selection, reports inventory changes and does not silently choose a fallback", async () => {
    const f = fixture();
    render(<InputDevicePicker value="missing-private" onSelect={f.onSelect} disabled={false} />);
    fireEvent.click(screen.getByRole("button", { name: "读取麦克风列表" }));
    await screen.findByRole("option", { name: "<b>Microphone</b>" });
    expect(screen.getByLabelText("输入麦克风")).toHaveValue("missing-private");
    expect(screen.getByRole("status")).toHaveTextContent("所选设备不在当前列表");
    act(() => {
      f.media.dispatchEvent(new Event("devicechange"));
    });
    expect(screen.getByRole("status")).toHaveTextContent("设备列表可能已变化");
    expect(f.enumerateDevices).toHaveBeenCalledTimes(1);
    expect(f.onSelect).not.toHaveBeenCalled();
  });
  it("shows a bounded inventory failure without exposing browser errors or identifiers", async () => {
    const f = fixture();
    f.enumerateDevices.mockRejectedValue(Error("private-device/internal-error"));
    render(<InputDevicePicker value={null} onSelect={f.onSelect} disabled={false} />);
    fireEvent.click(screen.getByRole("button", { name: "读取麦克风列表" }));
    expect(await screen.findByRole("status")).toHaveTextContent("无法读取麦克风列表");
    expect(document.body).not.toHaveTextContent("internal-error");
    expect(f.media.getUserMedia).not.toHaveBeenCalled();
  });
  it("discards an enumeration that finishes after leaving the active host", async () => {
    const f = fixture();
    let resolve!: (devices: unknown[]) => void;
    f.enumerateDevices.mockReturnValue(
      new Promise((yes) => {
        resolve = yes;
      }),
    );
    const { unmount } = render(
      <InputDevicePicker value={null} onSelect={f.onSelect} disabled={false} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "读取麦克风列表" }));
    unmount();
    await act(async () => {
      resolve([{ kind: "audioinput", deviceId: "late", label: "Late microphone" }]);
      await Promise.resolve();
    });
    expect(screen.queryByRole("option", { name: "Late microphone" })).not.toBeInTheDocument();
    expect(f.onSelect).not.toHaveBeenCalled();
  });
});
