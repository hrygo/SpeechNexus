import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "./App";

describe("AdminUI shell", () => {
  it("states the shared boundary and pending contract work", () => {
    render(<App />);

    expect(screen.getByRole("heading", { level: 1, name: "运行事实管理" })).toBeInTheDocument();
    expect(screen.getByLabelText("部署数据边界")).toHaveTextContent("没有登录与角色隔离");
    expect(screen.getAllByText("等待 P0 契约")).toHaveLength(1);
  });
});
