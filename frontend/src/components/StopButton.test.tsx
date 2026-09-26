/**
 * StopButton.test.tsx — the control three panels share.
 *
 * Small on purpose, because the claims worth testing are the two that are easy to
 * lose in a restyle: the accessible name stays "Stop" (the agent console, the review
 * panel and the writer all promise the same verb), and the button is a real button
 * with `type="button"` so pressing it inside a form does not submit the form.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { StopButton } from "./StopButton";

describe("StopButton", () => {
  it("is a button named Stop that calls back once per click", () => {
    const onClick = vi.fn();
    render(<StopButton onClick={onClick} />);

    const button = screen.getByRole("button", { name: "Stop" });
    expect(button).toHaveAttribute("type", "button");

    fireEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("keeps the accessible name when the label changes", () => {
    render(<StopButton onClick={() => {}} label="Stop review" />);
    expect(screen.getByRole("button", { name: "Stop" })).toHaveTextContent("Stop review");
    expect(screen.getByRole("button", { name: "Stop" })).toHaveAccessibleName("Stop");
  });

  it("says what stopping does, in the title", () => {
    render(<StopButton onClick={() => {}} />);
    expect(screen.getByRole("button", { name: "Stop" })).toHaveAttribute(
      "title",
      expect.stringContaining("model"),
    );
  });
});
