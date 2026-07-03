import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useToast } from "./useToast";

describe("useToast", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("starts with no toasts", () => {
    const { result } = renderHook(() => useToast());
    expect(result.current.toasts).toEqual([]);
  });

  it("adds a toast with the given message and type", () => {
    const { result } = renderHook(() => useToast());

    act(() => {
      result.current.addToast("Saved successfully", "success");
    });

    expect(result.current.toasts).toHaveLength(1);
    expect(result.current.toasts[0]).toMatchObject({
      message: "Saved successfully",
      type: "success",
    });
  });

  it("defaults the type to info when not specified", () => {
    const { result } = renderHook(() => useToast());

    act(() => {
      result.current.addToast("Just so you know");
    });

    expect(result.current.toasts[0]?.type).toBe("info");
  });

  it("auto-removes a toast after 4000ms", () => {
    const { result } = renderHook(() => useToast());

    act(() => {
      result.current.addToast("Transient message");
    });
    expect(result.current.toasts).toHaveLength(1);

    act(() => {
      vi.advanceTimersByTime(4000);
    });
    expect(result.current.toasts).toHaveLength(0);
  });

  it("removeToast removes a toast immediately by id", () => {
    const { result } = renderHook(() => useToast());

    act(() => {
      result.current.addToast("First");
      result.current.addToast("Second");
    });
    expect(result.current.toasts).toHaveLength(2);

    const firstId = result.current.toasts[0]!.id;
    act(() => {
      result.current.removeToast(firstId);
    });

    expect(result.current.toasts).toHaveLength(1);
    expect(result.current.toasts[0]?.message).toBe("Second");
  });
});
