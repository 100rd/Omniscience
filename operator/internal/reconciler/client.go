// HTTP client for the Omniscience read API surface added in issue #163.
//
// The reconciler queries the server's operator-scoped read endpoint to
// discover which entities the graph believes exist for a given
// (workspace_id, cluster_id, kind) tuple. The endpoint is bearer-
// authenticated with a token that carries the workspace_id; the server
// 403s on any mismatch between the token's workspace and the
// workspace_id query parameter — this is the load-bearing ACL gate.
package reconciler

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
)

// EntitiesAPIClient is the narrow interface the reconciler needs from the
// Omniscience read API. Defined as an interface so tests can substitute a
// stubbed implementation without spinning up an httptest.Server in the
// hot path of pure unit tests. The reconciler test suite does spin up an
// httptest.Server because it exercises end-to-end paginated walks.
type EntitiesAPIClient interface {
	// ListExternalIDs returns every external_id the graph believes exists
	// for (workspaceID, clusterID, kind), filtered server-side to entities
	// emitted by the operator (emitter=k8s-operator). The implementation
	// MUST follow pagination cursors to completion.
	ListExternalIDs(
		ctx context.Context,
		workspaceID uuid.UUID,
		clusterID uuid.UUID,
		kind string,
	) ([]string, error)
}

// HTTPEntitiesClient is the production EntitiesAPIClient. Configured at
// startup from env vars (OMNISCIENCE_API_BASE_URL, OMNISCIENCE_API_BEARER_TOKEN).
// A single client instance is reused across reconcile cycles so HTTP
// keep-alive and connection pooling carry across runs.
type HTTPEntitiesClient struct {
	// baseURL is the absolute server URL, e.g. "https://omniscience.example".
	// The endpoint path "/api/v1/operator/entities" is appended at request
	// time. Trailing slash is normalised away in NewHTTPEntitiesClient so
	// callers cannot accidentally produce double-slash URLs.
	baseURL string

	// bearerToken authenticates the operator. MUST come from a Secret-
	// mounted file (see ADR-0007 §ACL); never from cluster-side state.
	// The token's workspace_id field gates the server's ACL check —
	// passing the wrong token is a 403, not a silent cross-workspace read.
	bearerToken string

	// httpClient is the underlying transport. Tests inject one with a
	// custom RoundTripper; production uses NewHTTPEntitiesClient's default.
	httpClient *http.Client

	// pageLimit is the per-request entity cap. Server caps it server-side
	// too; this is the operator's polite cap that minimises memory
	// transient on the operator side. 500 is a balance between request
	// count and JSON-decode buffer size.
	pageLimit int
}

// HTTPEntitiesClientOption customises a client at construction time. Used
// in tests to inject the httptest.Server's HTTP client.
type HTTPEntitiesClientOption func(*HTTPEntitiesClient)

// WithHTTPClient replaces the underlying *http.Client. Tests pass the one
// from httptest.NewServer().Client(); production code never needs this.
func WithHTTPClient(c *http.Client) HTTPEntitiesClientOption {
	return func(h *HTTPEntitiesClient) {
		h.httpClient = c
	}
}

// WithPageLimit overrides the default page size. Tests use this to force
// pagination boundary cases without needing thousands of stubbed entities.
func WithPageLimit(n int) HTTPEntitiesClientOption {
	return func(h *HTTPEntitiesClient) {
		if n > 0 {
			h.pageLimit = n
		}
	}
}

// NewHTTPEntitiesClient validates inputs and returns a configured client.
// The bearer token is held in memory and never logged — even on error
// paths the implementation only logs the request URL, never the token.
func NewHTTPEntitiesClient(baseURL, bearerToken string, opts ...HTTPEntitiesClientOption) (*HTTPEntitiesClient, error) {
	if baseURL == "" {
		return nil, errors.New("reconciler: api base url is required")
	}
	if bearerToken == "" {
		return nil, errors.New("reconciler: api bearer token is required")
	}
	parsed, err := url.Parse(baseURL)
	if err != nil {
		return nil, fmt.Errorf("reconciler: invalid api base url: %w", err)
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return nil, fmt.Errorf("reconciler: api base url must be http(s): got %q", parsed.Scheme)
	}
	c := &HTTPEntitiesClient{
		baseURL:     strings.TrimRight(baseURL, "/"),
		bearerToken: bearerToken,
		// 30s per-request timeout — generous enough to cover paginated
		// walks against a slow server, tight enough that a wedged
		// reconcile cycle never holds up a leader-election lease.
		httpClient: &http.Client{Timeout: 30 * time.Second},
		pageLimit:  500,
	}
	for _, opt := range opts {
		opt(c)
	}
	return c, nil
}

// entitiesPage is the JSON shape returned by the read API. The field names
// match the server-side Pydantic model verbatim. Forward-compatible: extra
// fields the server adds later are ignored by Go's json decoder.
type entitiesPage struct {
	ExternalIDs []string `json:"external_ids"`
	NextCursor  string   `json:"next_cursor"`
}

// ListExternalIDs walks every page of the read API and returns the
// concatenated external_id list for the given (workspaceID, clusterID,
// kind) scope. Server-side filtering on emitter=k8s-operator is mandatory;
// the client trusts the server to enforce it (the server is the source of
// truth for the emitter filter, not the client).
//
// Pagination protocol: an empty next_cursor on a response terminates the
// walk. The client refuses to follow more than a hard cap of pages so a
// server bug producing infinite pagination cannot wedge the reconciler.
func (c *HTTPEntitiesClient) ListExternalIDs(
	ctx context.Context,
	workspaceID uuid.UUID,
	clusterID uuid.UUID,
	kind string,
) ([]string, error) {
	if kind == "" {
		return nil, errors.New("reconciler: kind is required")
	}

	const maxPages = 1000 // 1000 * pageLimit(500) = 500k entities per kind, plenty.
	var (
		out     []string
		cursor  string
		pageIdx int
	)
	for {
		page, err := c.fetchPage(ctx, workspaceID, clusterID, kind, cursor)
		if err != nil {
			return nil, err
		}
		out = append(out, page.ExternalIDs...)
		if page.NextCursor == "" {
			return out, nil
		}
		cursor = page.NextCursor
		pageIdx++
		if pageIdx >= maxPages {
			return nil, fmt.Errorf("reconciler: pagination exceeded %d pages for kind=%s", maxPages, kind)
		}
	}
}

// fetchPage performs one HTTP GET and decodes the JSON response. Errors
// are wrapped with the request URL (no token, no body content) so log
// scrapers can trace failures without exposing the bearer.
func (c *HTTPEntitiesClient) fetchPage(
	ctx context.Context,
	workspaceID uuid.UUID,
	clusterID uuid.UUID,
	kind string,
	cursor string,
) (*entitiesPage, error) {
	q := url.Values{}
	q.Set("workspace_id", workspaceID.String())
	q.Set("cluster_id", clusterID.String())
	q.Set("kind", kind)
	q.Set("limit", strconv.Itoa(c.pageLimit))
	if cursor != "" {
		q.Set("cursor", cursor)
	}

	reqURL := c.baseURL + "/api/v1/operator/entities?" + q.Encode()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, reqURL, nil)
	if err != nil {
		return nil, fmt.Errorf("reconciler: build request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+c.bearerToken)
	req.Header.Set("Accept", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		// Note: deliberately do NOT wrap with the URL's query string in
		// case it ever embeds anything sensitive. Path is sufficient.
		return nil, fmt.Errorf("reconciler: GET %s/api/v1/operator/entities: %w", c.baseURL, err)
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode != http.StatusOK {
		// Read a small bounded prefix of the body so a server-side bug
		// returning a 100MiB error page cannot OOM the operator.
		bodyPrefix, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return nil, fmt.Errorf(
			"reconciler: GET %s/api/v1/operator/entities returned %d: %s",
			c.baseURL, resp.StatusCode, strings.TrimSpace(string(bodyPrefix)),
		)
	}

	var page entitiesPage
	if derr := json.NewDecoder(resp.Body).Decode(&page); derr != nil {
		return nil, fmt.Errorf("reconciler: decode response: %w", derr)
	}
	return &page, nil
}
