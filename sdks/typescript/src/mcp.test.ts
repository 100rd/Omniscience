import { describe, expect, it } from "vitest";
import { ConfigurationError, NetworkError } from "./errors.js";
import type { FetchFn } from "./rest.js";
import { OmniscienceMCP } from "./mcp.js";

function mcpTextResponse(id: number, payload: unknown): Response {
  return new Response(
    JSON.stringify({
      jsonrpc: "2.0",
      id,
      result: { content: [{ type: "text", text: JSON.stringify(payload) }] },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

function mcpErrorResponse(id: number, code: number, message: string): Response {
  return new Response(
    JSON.stringify({ jsonrpc: "2.0", id, error: { code, message } }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

describe("OmniscienceMCP construction", () => {
  it("requires a url for http transport", () => {
    expect(
      () => new OmniscienceMCP({ transport: "http", token: "tok" }),
    ).toThrow(ConfigurationError);
  });

  it("requires a token for http transport", () => {
    expect(
      () =>
        new OmniscienceMCP({
          transport: "http",
          url: "https://example.com",
        }),
    ).toThrow(ConfigurationError);
  });
});

describe("OmniscienceMCP http transport", () => {
  it("posts a tools/call JSON-RPC request to <url>/mcp with a bearer token", async () => {
    let capturedUrl = "";
    let capturedInit: RequestInit | undefined;
    const fetch: FetchFn = async (input, init) => {
      capturedUrl = typeof input === "string" ? input : input.toString();
      capturedInit = init;
      const body = JSON.parse(String(init?.body)) as { id: number };
      return mcpTextResponse(body.id, { hits: [] });
    };

    const mcp = new OmniscienceMCP(
      { transport: "http", url: "https://example.com/", token: "tok" },
      fetch,
    );

    const result = await mcp.search("connection pooling", { top_k: 3 });

    expect(result).toEqual({ hits: [] });
    expect(capturedUrl).toBe("https://example.com/mcp");
    const headers = capturedInit?.headers as Record<string, string>;
    expect(headers["Authorization"]).toBe("Bearer tok");
    const sentBody = JSON.parse(String(capturedInit?.body));
    expect(sentBody.method).toBe("tools/call");
    expect(sentBody.params).toEqual({
      name: "search",
      arguments: { query: "connection pooling", top_k: 3 },
    });
  });

  it("unwraps list_sources from the { sources: [...] } envelope", async () => {
    const fetch: FetchFn = async (_input, init) => {
      const body = JSON.parse(String(init?.body)) as { id: number };
      return mcpTextResponse(body.id, {
        sources: [{ id: "s1", name: "main-repo" }],
      });
    };

    const mcp = new OmniscienceMCP(
      { transport: "http", url: "https://example.com", token: "tok" },
      fetch,
    );

    const sources = await mcp.listSources();
    expect(sources).toEqual([{ id: "s1", name: "main-repo" }]);
  });

  it("throws NetworkError when the MCP response contains a JSON-RPC error", async () => {
    const fetch: FetchFn = async (_input, init) => {
      const body = JSON.parse(String(init?.body)) as { id: number };
      return mcpErrorResponse(body.id, -32000, "tool execution failed");
    };

    const mcp = new OmniscienceMCP(
      { transport: "http", url: "https://example.com", token: "tok" },
      fetch,
    );

    await expect(mcp.search("x")).rejects.toBeInstanceOf(NetworkError);
    await expect(mcp.search("x")).rejects.toThrow(/tool execution failed/);
  });

  it("throws NetworkError when the underlying fetch rejects", async () => {
    const fetch: FetchFn = async () => {
      throw new Error("network down");
    };

    const mcp = new OmniscienceMCP(
      { transport: "http", url: "https://example.com", token: "tok" },
      fetch,
    );

    await expect(mcp.search("x")).rejects.toBeInstanceOf(NetworkError);
  });
});
