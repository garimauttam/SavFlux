import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TopBar } from "./TopBar";
import type { ModelStatus } from "../../types/workspace";
const props = {
  repos: [{ url: "https://github.com/o/r", name: "o/r" }],
  activeRepoUrl: "https://github.com/o/r",
  onSelectRepo: vi.fn(),
  branch: "main",
  branches: ["main", "feature"],
  onSelectBranch: vi.fn(),
  branchesLoading: false,
  indexCount: 1,
  github: null,
  models: {
    available: true,
    reachable: true,
    chat_model: "coder:7b",
    models: [{ name: "coder:7b" }, { name: "other:7b" }],
    suggested: [],
  } as unknown as ModelStatus,
  modelsBusy: false,
  onSelectModel: vi.fn(),
  onConnectGitHub: vi.fn(),
  onSignOut: vi.fn(),
  onOpenProfile: vi.fn(),
  onOpenCommandPalette: vi.fn(),
  contextOpen: false,
  onToggleContext: vi.fn(),
  isDark: true,
  onToggleTheme: vi.fn(),
};
describe("header menus", () => {
  it("does not dismiss the model menu when its input receives a pointer click", () => {
    render(<TopBar {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "coder:7b" }));
    const input = screen.getByRole("textbox", { name: "Filter models" });
    fireEvent.mouseDown(input);
    fireEvent.change(input, { target: { value: "coder" } });
    expect(
      screen.getByRole("option", { name: "coder:7b" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "other:7b" })).toBeNull();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("textbox", { name: "Filter models" })).toBeNull();
  });
  it("selects a branch after mousedown and closes on outside click", () => {
    render(<TopBar {...props} />);
    fireEvent.click(
      screen.getByRole("button", { name: "Selected branch: main" }),
    );
    const option = screen.getByRole("option", { name: "feature" });
    fireEvent.mouseDown(option);
    fireEvent.click(option);
    expect(props.onSelectBranch).toHaveBeenCalledWith("feature");
    fireEvent.click(
      screen.getByRole("button", { name: "Selected branch: main" }),
    );
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("listbox")).toBeNull();
  });
  it("provides a profile entry separate from the GitHub connection", () => {
    render(<TopBar {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "Connect GitHub" }));
    fireEvent.click(screen.getByRole("button", { name: "Profile & settings" }));
    expect(props.onOpenProfile).toHaveBeenCalledOnce();
  });
});
