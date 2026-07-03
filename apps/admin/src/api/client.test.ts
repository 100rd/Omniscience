import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "./client";

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("ApiClient", () => {
  beforeEach(() => {
    document.cookie =
      "csrf_token=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("does not attach an X-CSRF-Token header for GET requests", async () => {
    document.cookie = "csrf_token=abc123";
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ status: "ok" }));
    vi.stubGlobal("fetch", fetchMock);

    const client = new ApiClient();
    await client.health();

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = init.headers as Record<string, string>;
    expect(headers["X-CSRF-Token"]).toBeUndefined();
    expect(init.credentials).toBe("include");
  });

  it("attaches the X-CSRF-Token header from the cookie on mutating requests", async () => {
    document.cookie = "csrf_token=abc123";
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ id: "src-1" }));
    vi.stubGlobal("fetch", fetchMock);

    const client = new ApiClient();
    await client.createSource({ type: "git", name: "main-repo" });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/sources");
    const headers = init.headers as Record<string, string>;
    expect(headers["X-CSRF-Token"]).toBe("abc123");
  });

  it("omits the X-CSRF-Token header when no csrf_token cookie is set", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ id: "src-1" }));
    vi.stubGlobal("fetch", fetchMock);

    const client = new ApiClient();
    await client.deleteSource("src-1");

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = init.headers as Record<string, string>;
    expect(headers["X-CSRF-Token"]).toBeUndefined();
  });

  it("builds a query string for listSources filters", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    const client = new ApiClient();
    await client.listSources({ source_type: "git", status: "active" });

    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe("/api/v1/sources?source_type=git&status=active");
  });

  it("returns undefined for a 204 No Content response", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    const client = new ApiClient();
    await expect(client.deleteSource("src-1")).resolves.toBeUndefined();
  });

  it("throws ApiError with the string detail field on a non-ok response", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(
        { detail: "Source not found" },
        { status: 404, statusText: "Not Found" },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new ApiClient();
    await expect(client.getSource("missing")).rejects.toBeInstanceOf(
      ApiError,
    );
    await expect(client.getSource("missing")).rejects.toMatchObject({
      status: 404,
      detail: "Source not found",
    });
  });

  it("extracts a nested detail.message field on a non-ok response", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(
        { detail: { message: "Validation failed" } },
        { status: 422, statusText: "Unprocessable Entity" },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new ApiClient();
    await expect(client.createSource({ type: "git", name: "x" })).rejects
      .toMatchObject({
        status: 422,
        detail: "Validation failed",
      });
  });
});
