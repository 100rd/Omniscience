/**
 * Error classes for @omniscience/sdk.
 *
 * All errors thrown by the client extend OmniscienceError so callers can do:
 *
 *   import { OmniscienceError } from "@omniscience/sdk";
 *   if (err instanceof OmniscienceError) { ... }
 */

import type { OmniscienceErrorBody } from "./types.js";

/**
 * Base error class for all Omniscience SDK errors.
 */
export class OmniscienceError extends Error {
  /** API error code (e.g. "unauthorized", "source_not_found"). */
  readonly code: string;
  /** HTTP status code when the error originated from the REST API. */
  readonly status: number | undefined;
  /** Additional error details from the server payload. */
  readonly details: Record<string, unknown> | undefined;

  constructor(
    message: string,
    code: string,
    status?: number,
    details?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "OmniscienceError";
    this.code = code;
    this.status = status;
    this.details = details;
    // Maintain correct prototype chain in transpiled ES5
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

/** Raised when the server returns a 4xx or 5xx response. */
export class ApiError extends OmniscienceError {
  constructor(status: number, body: OmniscienceErrorBody) {
    super(body.error.message, body.error.code, status, body.error.details);
    this.name = "ApiError";
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

/** Raised when the request could not be sent (network failure, timeout). */
export class NetworkError extends OmniscienceError {
  /** The underlying error that caused the network failure. */
  override readonly cause: Error;

  constructor(message: string, cause: Error) {
    super(message, "network_error");
    this.name = "NetworkError";
    this.cause = cause;
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

/** Raised for invalid SDK configuration (missing required options, etc.). */
export class ConfigurationError extends OmniscienceError {
  constructor(message: string) {
    super(message, "configuration_error");
    this.name = "ConfigurationError";
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

/**
 * Parse a fetch Response into an ApiError, falling back to a generic message
 * when the body is not valid JSON or does not match the expected shape.
 */
export async function parseApiError(response: Response): Promise<ApiError> {
  let body: unknown;

  try {
    body = await response.json();
  } catch {
    // Non-JSON body
    return new ApiError(response.status, {
      error: {
        code: "http_error",
        message: `HTTP ${response.status} ${response.statusText}`,
      },
    });
  }

  if (isErrorBody(body)) {
    return new ApiError(response.status, body);
  }

  return new ApiError(response.status, {
    error: {
      code: "http_error",
      message: `HTTP ${response.status} ${response.statusText}`,
    },
  });
}

function isErrorBody(value: unknown): value is OmniscienceErrorBody {
  return (
    typeof value === "object" &&
    value !== null &&
    "error" in value &&
    typeof (value as Record<string, unknown>)["error"] === "object"
  );
}
