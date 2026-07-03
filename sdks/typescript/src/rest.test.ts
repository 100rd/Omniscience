import { describe, expect, it, vi } from "vitest";
import { ApiError, NetworkError } from "./errors.js";
import { OmniscienceClient, type FetchFn } from "./rest.js";

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

function makeFetch(
  handler: (url: string, init?: RequestInit) => Response | Promise<Response>,
): { fetch: FetchFn; calls: Array<{ url: string; init?: RequestInit }> } {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const fetch: FetchFn = async (input, init) => {
    const url = typeof input === "string" ? input : input.toString();
    calls.push({ url, init });
    return handler(url, init);
  };
  return { fetch, calls };
}

describe("OmniscienceClient", () => {
  it("strips a trailing slash from baseUrl", async () => {
    const { fetch, calls } = makeFetch(() => jsonResponse({ hits: [] }));
    const client = new OmniscienceClient({
      baseUrl: "https://example.com/",
      token: "tok",
      fetch,
    });

    await client.search({ query: "x" });

    expect(calls[0]?.url).toBe("https://example.com/api/v1/search");
  });

  it("sends an Authorization bearer header for authenticated requests", async () => {
    const { fetch, calls } = makeFetch(() => jsonResponse({ hits: [] }));
    const client = new OmniscienceClient({
      baseUrl: "https://example.com",
      token: "secret-token",
      fetch,
    });

    await client.search({ query: "x" });

    const headers = calls[0]?.init?.headers as Record<string, string>;
    expect(headers["Authorization"]).toBe("Bearer secret-token");
  });

  it("does not send an Authorization header for health()", async () => {
    const { fetch, calls } = makeFetch(() =>
      jsonResponse({ status: "ok", version: "1.0.0" }),
    );
    const client = new OmniscienceClient({
      baseUrl: "https://example.com",
      token: "secret-token",
      fetch,
    });

    const health = await client.health();

    expect(health).toEqual({ status: "ok", version: "1.0.0" });
    const headers = (calls[0]?.init?.headers ?? {}) as Record<string, string>;
    expect(headers["Authorization"]).toBeUndefined();
  });

  it("builds a query string from listSources filters, skipping undefined", async () => {
    const { fetch, calls } = makeFetch(() => jsonResponse([]));
    const client = new OmniscienceClient({
      baseUrl: "https://example.com",
      token: "tok",
      fetch,
    });

    await client.listSources({ type: "git", status: undefined });

    expect(calls[0]?.url).toBe(
      "https://example.com/api/v1/sources?type=git",
    );
  });

  it("omits the query string entirely when listSources is called without params", async () => {
    const { fetch, calls } = makeFetch(() => jsonResponse([]));
    const client = new OmniscienceClient({
      baseUrl: "https://example.com",
      token: "tok",
      fetch,
    });

    await client.listSources();

    expect(calls[0]?.url).toBe("https://example.com/api/v1/sources");
  });

  it("throws ApiError on a non-2xx JSON error response", async () => {
    const { fetch } = makeFetch(() =>
      jsonResponse(
        { error: { code: "source_not_found", message: "nope" } },
        { status: 404, statusText: "Not Found" },
      ),
    );
    const client = new OmniscienceClient({
      baseUrl: "https://example.com",
      token: "tok",
      fetch,
    });

    await expect(client.getSource("missing")).rejects.toMatchObject({
      code: "source_not_found",
      status: 404,
    });
    await expect(client.getSource("missing")).rejects.toBeInstanceOf(
      ApiError,
    );
  });

  it("throws NetworkError when the underlying fetch rejects", async () => {
    const fetch: FetchFn = async () => {
      throw new Error("ECONNREFUSED");
    };
    const client = new OmniscienceClient({
      baseUrl: "https://example.com",
      token: "tok",
      fetch,
    });

    await expect(client.search({ query: "x" })).rejects.toBeInstanceOf(
      NetworkError,
    );
  });

  it("returns undefined for 204 No Content responses", async () => {
    const { fetch } = makeFetch(
      () => new Response(null, { status: 204 }),
    );
    const client = new OmniscienceClient({
      baseUrl: "https://example.com",
      token: "tok",
      fetch,
    });

    await expect(client.deleteSource("id-1")).resolves.toBeUndefined();
  });

  it("merges extra headers for deliverWebhook without an Authorization header", async () => {
    const { fetch, calls } = makeFetch(() => jsonResponse({ run_id: "r1" }));
    const client = new OmniscienceClient({
      baseUrl: "https://example.com",
      token: "tok",
      fetch,
    });

    await client.deliverWebhook("github-source", { ping: true }, {
      "X-Hub-Signature-256": "sha256=deadbeef",
    });

    const headers = calls[0]?.init?.headers as Record<string, string>;
    expect(headers["X-Hub-Signature-256"]).toBe("sha256=deadbeef");
    expect(headers["Authorization"]).toBeUndefined();
    expect(calls[0]?.url).toBe(
      "https://example.com/api/v1/ingest/webhook/github-source",
    );
  });
});
