import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ActivityRail } from "./ActivityRail";

const props = {
  active: "review" as const,
  onSelect: vi.fn(),
  hasIndex: true,
  githubConnected: true,
  onConnectGitHub: vi.fn(),
  onOpenSettings: vi.fn(),
};
beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("savflux:railPinned", "1");
  vi.clearAllMocks();
});

describe("sidebar controls", () => {
  it("places the toggle beside Work above the scrollable navigation", () => {
    render(<ActivityRail {...props} />);
    const toggle = screen.getByRole("button", { name: "Collapse sidebar" });
    const navigation = screen.getByRole("navigation", { name: "Sections" });
    expect(toggle.parentElement).toHaveTextContent("Work");
    expect(navigation).not.toContainElement(toggle);
    expect(toggle).toHaveAttribute("aria-controls", navigation.id);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(
      toggle.compareDocumentPosition(navigation) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("stays collapsed under the pointer until explicitly expanded, retaining focus", async () => {
    const user = userEvent.setup();
    render(<ActivityRail {...props} />);
    const rail = screen.getByTestId("activity-rail");
    const toggle = screen.getByRole("button", { name: "Collapse sidebar" });
    toggle.focus();
    await user.keyboard("{Enter}");
    fireEvent.mouseEnter(rail);
    expect(rail).toHaveAttribute("data-expanded", "false");
    expect(localStorage.getItem("savflux:railPinned")).toBe("0");
    expect(
      screen.getByRole("button", { name: "Expand sidebar" }),
    ).toHaveFocus();
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.keyboard(" ");
    expect(rail).toHaveAttribute("data-expanded", "true");
    expect(localStorage.getItem("savflux:railPinned")).toBe("1");
  });

  it("shows one labeled profile control in the footer with the correct action and selection", () => {
    render(<ActivityRail {...props} active="profile" />);
    const profiles = screen.getAllByRole("tab", { name: "Profile" });
    expect(profiles).toHaveLength(1);
    const profile = profiles[0];
    expect(within(profile).getByText("Profile")).toBeVisible();
    expect(within(profile).getByText("Account & settings")).toBeVisible();
    expect(profile).toHaveAttribute("aria-selected", "true");
    expect(
      screen.getByRole("navigation", { name: "Sections" }),
    ).not.toContainElement(profile);
    fireEvent.click(profile);
    expect(props.onOpenSettings).toHaveBeenCalledOnce();
    expect(props.onSelect).not.toHaveBeenCalled();
  });

  it("restores collapsed width and keeps Profile and connection controls accessible", () => {
    localStorage.setItem("savflux:railPinned", "0");
    render(
      <ActivityRail {...props} hasIndex={false} githubConnected={false} />,
    );
    expect(
      screen.getByRole("button", { name: "Expand sidebar" }),
    ).toBeVisible();
    const profile = screen.getByRole("tab", { name: "Profile" });
    expect(profile).toHaveAttribute("title", "Profile — account & settings");
    expect(profile).toHaveAttribute("aria-selected", "false");
    expect(within(profile).queryByText("Profile")).toBeNull();
    fireEvent.click(profile);
    fireEvent.click(screen.getByRole("button", { name: "Add a repository" }));
    fireEvent.click(screen.getByRole("button", { name: "Connect GitHub" }));
    expect(props.onOpenSettings).toHaveBeenCalledOnce();
    expect(props.onSelect).toHaveBeenCalledWith("repos");
    expect(props.onConnectGitHub).toHaveBeenCalledOnce();
  });
});
