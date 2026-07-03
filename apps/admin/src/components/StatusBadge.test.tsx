import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StatusBadge } from "./StatusBadge";

describe("StatusBadge", () => {
  it("renders the raw value as its text content", () => {
    render(<StatusBadge value="active" />);
    expect(screen.getByText("active")).toBeInTheDocument();
  });

  it.each([
    ["active", "bg-success-bg"],
    ["ok", "bg-success-bg"],
    ["paused", "bg-warning-bg"],
    ["partial", "bg-warning-bg"],
    ["error", "bg-danger-bg"],
    ["running", "bg-info-bg"],
  ])("maps status %s to the %s semantic token", (value, expectedClass) => {
    render(<StatusBadge value={value} />);
    expect(screen.getByText(value)).toHaveClass(expectedClass);
  });

  it("falls back to the neutral token for an unrecognized value", () => {
    render(<StatusBadge value="unknown-status" />);
    const el = screen.getByText("unknown-status");
    expect(el).toHaveClass("bg-elevation-2");
    expect(el).toHaveClass("text-text-secondary");
  });
});
