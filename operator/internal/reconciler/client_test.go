// Tests for HTTPEntitiesClient — paginated walk, bearer auth, error paths.
// Each test spins up an httptest.Server and asserts the client's request
// shape and pagination behaviour.
package reconciler_test

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/google/uuid"

	"github.com/100rd/omniscience/operator/internal/reconciler"
)

// recorder captures requests so tests can assert on what the client sent.
type recorder struct {
	mu       sync.Mutex
	requests []*http.Request
}

func (r *recorder) record(req *http.Request) {
	r.mu.Lock()
	defer r.mu.Unlock()
	clone := req.Clone(req.Context())
	r.requests = append(r.requests, clone)
}

func (r *recorder) snapshot() []*http.Request {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]*http.Request, len(r.requests))
	copy(out, r.requests)
	return out
}

func TestHTTPEntitiesClient_HappyPath_Pagination(t *testing.T) {
	rec := &recorder{}
	wsID := uuid.MustParse("11111111-1111-1111-1111-111111111111")
	cidID := uuid.MustParse("22222222-2222-2222-2222-222222222222")

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		rec.record(req)

		// Assert authentication header.
		if got, want := req.Header.Get("Authorization"), "Bearer test-token"; got != want {
			t.Errorf("Authorization header = %q, want %q", got, want)
		}

		// Assert query params.
		q := req.URL.Query()
		if got := q.Get("workspace_id"); got != wsID.String() {
			t.Errorf("workspace_id = %q, want %q", got, wsID.String())
		}
		if got := q.Get("cluster_id"); got != cidID.String() {
			t.Errorf("cluster_id = %q, want %q", got, cidID.String())
		}
		if got := q.Get("kind"); got != "Pod" {
			t.Errorf("kind = %q, want Pod", got)
		}

		w.Header().Set("Content-Type", "application/json")
		// Three pages: cursor=="" → page1, "p2" → page2, "p3" → page3 with empty next.
		switch q.Get("cursor") {
		case "":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"external_ids": []string{"k8s_resource/cid/Pod/ns/a", "k8s_resource/cid/Pod/ns/b"},
				"next_cursor":  "p2",
			})
		case "p2":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"external_ids": []string{"k8s_resource/cid/Pod/ns/c"},
				"next_cursor":  "p3",
			})
		case "p3":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"external_ids": []string{"k8s_resource/cid/Pod/ns/d"},
				"next_cursor":  "",
			})
		default:
			t.Fatalf("unexpected cursor %q", q.Get("cursor"))
		}
	}))
	defer server.Close()

	c, err := reconciler.NewHTTPEntitiesClient(server.URL, "test-token", reconciler.WithHTTPClient(server.Client()))
	if err != nil {
		t.Fatalf("client init: %v", err)
	}
	got, err := c.ListExternalIDs(context.Background(), wsID, cidID, "Pod")
	if err != nil {
		t.Fatalf("ListExternalIDs: %v", err)
	}
	want := []string{
		"k8s_resource/cid/Pod/ns/a",
		"k8s_resource/cid/Pod/ns/b",
		"k8s_resource/cid/Pod/ns/c",
		"k8s_resource/cid/Pod/ns/d",
	}
	if len(got) != len(want) {
		t.Fatalf("len = %d, want %d (got=%v)", len(got), len(want), got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("got[%d] = %q, want %q", i, got[i], want[i])
		}
	}
	if n := len(rec.snapshot()); n != 3 {
		t.Errorf("server saw %d requests, want 3", n)
	}
}

func TestHTTPEntitiesClient_RejectsHTTPError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"detail":{"code":"forbidden","message":"workspace mismatch"}}`))
	}))
	defer server.Close()

	c, err := reconciler.NewHTTPEntitiesClient(server.URL, "test-token", reconciler.WithHTTPClient(server.Client()))
	if err != nil {
		t.Fatalf("client init: %v", err)
	}
	_, err = c.ListExternalIDs(context.Background(), uuid.New(), uuid.New(), "Pod")
	if err == nil {
		t.Fatalf("expected error on 403, got nil")
	}
	if !strings.Contains(err.Error(), "403") {
		t.Errorf("error must mention 403; got: %v", err)
	}
	// Verify the bearer token is NEVER in the wrapped error message.
	if strings.Contains(err.Error(), "test-token") {
		t.Errorf("error must not leak bearer token; got: %v", err)
	}
}

func TestHTTPEntitiesClient_RejectsMalformedJSON(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte("not json"))
	}))
	defer server.Close()

	c, err := reconciler.NewHTTPEntitiesClient(server.URL, "tok", reconciler.WithHTTPClient(server.Client()))
	if err != nil {
		t.Fatalf("client init: %v", err)
	}
	_, err = c.ListExternalIDs(context.Background(), uuid.New(), uuid.New(), "Pod")
	if err == nil {
		t.Fatalf("expected decode error, got nil")
	}
}

func TestHTTPEntitiesClient_PaginationCapped(t *testing.T) {
	// Force a server that always returns next_cursor → pagination loop must
	// terminate via the maxPages cap, never hang.
	var calls int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		atomic.AddInt32(&calls, 1)
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprintf(w, `{"external_ids":["x"],"next_cursor":"%d"}`, atomic.LoadInt32(&calls))
	}))
	defer server.Close()

	c, err := reconciler.NewHTTPEntitiesClient(server.URL, "tok", reconciler.WithHTTPClient(server.Client()))
	if err != nil {
		t.Fatalf("client init: %v", err)
	}
	_, err = c.ListExternalIDs(context.Background(), uuid.New(), uuid.New(), "Pod")
	if err == nil {
		t.Fatalf("expected pagination cap error, got nil")
	}
	if !strings.Contains(err.Error(), "pagination exceeded") {
		t.Errorf("error must mention pagination cap; got: %v", err)
	}
}

func TestNewHTTPEntitiesClient_ValidatesInputs(t *testing.T) {
	if _, err := reconciler.NewHTTPEntitiesClient("", "tok"); err == nil {
		t.Errorf("expected error on empty url")
	}
	if _, err := reconciler.NewHTTPEntitiesClient("https://x", ""); err == nil {
		t.Errorf("expected error on empty token")
	}
	if _, err := reconciler.NewHTTPEntitiesClient("ftp://x", "tok"); err == nil {
		t.Errorf("expected error on non-http scheme")
	}
}
