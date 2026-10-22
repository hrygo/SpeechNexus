import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "./App";

describe("workbench shell", () => {
  it("does not present shared storage as personal isolation", () => {
    render(<App />);

    expect(screen.getByRole("heading", { level: 1, name: "语音任务集中完成" })).toBeInTheDocument();
    expect(screen.getByLabelText("部署数据边界")).toHaveTextContent("不代表服务端按用户私有保存");
    expect(screen.getByText("工程外壳已就绪")).toBeInTheDocument();
  });
});
