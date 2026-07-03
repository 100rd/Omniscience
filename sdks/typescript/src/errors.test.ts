import { describe, expect, it } from "vitest";
import {
  ApiError,
  ConfigurationError,
  NetworkError,
  OmniscienceError,
  parseApiError,
} from "./errors.js";

describe("OmniscienceError", () => {
  it("carries code, status and details, and is an instanceof Error", () => {
    const err = new OmniscienceError("boom", "some_code", 418, { foo: "bar" });

    expect(err).toBeInstanceOf(Error);
    expect(err).toBeInstanceOf(OmniscienceError);
    expect(err.message).toBe("boom");
    expect(err.code).toBe("some_code");
    expect(err.status).toBe(418);
    expect(err.details).toEqual({ foo: "bar" });
    expect(err.name).toBe("OmniscienceError");
  });

  it("defaults status and details to undefined", () => {
    const err = new OmniscienceError("boom", "some_code");
    expect(err.status).toBeUndefined();
    expect(err.details).toBeUndefined();
  });
});

describe("ApiError", () => {
  it("extends OmniscienceError with fields taken from the error body", () => {
    const err = new ApiError(404, {
      error: {
        code: "source_not_found",
        message: "Source not found",
        details: { id: "abc-123" },
      },
    });

    expect(err).toBeInstanceOf(OmniscienceError);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.name).toBe("ApiError");
    expect(err.status).toBe(404);
    expect(err.code).toBe("source_not_found");
    expect(err.message).toBe("Source not found");
    expect(err.details).toEqual({ id: "abc-123" });
  });
});

describe("NetworkError", () => {
  it("wraps the underlying cause and uses the network_error code", () => {
    const cause = new Error("ECONNREFUSED");
    const err = new NetworkError("Request failed", cause);

    expect(err).toBeInstanceOf(OmniscienceError);
    expect(err).toBeInstanceOf(NetworkError);
    expect(err.name).toBe("NetworkError");
    expect(err.code).toBe("network_error");
    expect(err.cause).toBe(cause);
  });
});

describe("ConfigurationError", () => {
  it("uses the configuration_error code", () => {
    const err = new ConfigurationError("baseUrl is required");

    expect(err).toBeInstanceOf(OmniscienceError);
    expect(err).toBeInstanceOf(ConfigurationError);
    expect(err.name).toBe("ConfigurationError");
    expect(err.code).toBe("configuration_error");
    expect(err.message).toBe("baseUrl is required");
  });
});

describe("parseApiError", () => {
  it("builds an ApiError from a well-formed JSON error body", async () => {
    const response = new Response(
      JSON.stringify({
        error: {
          code: "unauthorized",
          message: "Invalid token",
          details: { scope: "search" },
        },
      }),
      { status: 401, statusText: "Unauthorized" },
    );

    const err = await parseApiError(response);

    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(401);
    expect(err.code).toBe("unauthorized");
    expect(err.message).toBe("Invalid token");
    expect(err.details).toEqual({ scope: "search" });
  });

  it("falls back to a generic http_error when the body is not JSON", async () => {
    const response = new Response("<html>not json</html>", {
      status: 502,
      statusText: "Bad Gateway",
    });

    const err = await parseApiError(response);

    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(502);
    expect(err.code).toBe("http_error");
    expect(err.message).toContain("502");
  });

  it("falls back to a generic http_error when JSON does not match the error shape", async () => {
    const response = new Response(JSON.stringify({ unexpected: true }), {
      status: 500,
      statusText: "Internal Server Error",
    });

    const err = await parseApiError(response);

    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(500);
    expect(err.code).toBe("http_error");
  });
});
