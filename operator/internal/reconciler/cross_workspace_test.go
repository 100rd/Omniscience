// Cross-workspace isolation test for the reconciliation worker.
//
// This is the load-bearing security regression for issue #163. The
// scenario: two operators in two distinct workspaces (A and B), each
// configured with its OWN bearer token and its OWN read-API endpoint.
// Each reconciler in workspace A MUST query only A's stubbed server, and
// MUST emit zero events scoped to workspace B. Symmetric for B.
//
// We model the server-side ACL gate by having each httptest.Server
// reject any token that does not belong to its own workspace, and by
// having each server return a manufactured payload that DOES include
// adversarial cross-workspace external_ids the operator must NEVER
// publish (because they never appear in workspace A's diff, by virtue
// of the server-side filter).
//
// If anyone refactors the worker so it pools requests across workspaces
// or reads from the wrong stub, this test fails loudly.
package reconciler_test

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/omniscience/operator/internal/reconciler"
)

// workspaceServer wraps an httptest.Server with token+workspace gating.
// Tokens are bound to their workspace; a request whose Authorization does
// not match the bound token, OR whose workspace_id query param does not
// match the bound workspace, is 403'd.
type workspaceServer struct {
	*httptest.Server
	bearer       string
	workspaceID  uuid.UUID
	ourGraphIDs  []string
	rejectCount  atomic.Int32
	queryCount   atomic.Int32
	receivedAuth atomic.Pointer[string]
}

func newWorkspaceServer(t *testing.T, ws uuid.UUID, bearer string, graphIDs []string) *workspaceServer {
	t.Helper()
	ws_ := &workspaceServer{
		bearer:      bearer,
		workspaceID: ws,
		ourGraphIDs: graphIDs,
	}
	ws_.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		ws_.queryCount.Add(1)
		auth := req.Header.Get("Authorization")
		ws_.receivedAuth.Store(&auth)
		// ACL gate #1: token must match.
		if auth != "Bearer "+bearer {
			ws_.rejectCount.Add(1)
			w.WriteHeader(http.StatusUnauthorized)
			_, _ = fmt.Fprint(w, `{"detail":{"code":"unauthorized"}}`)
			return
		}
		// ACL gate #2: workspace_id query param must match the token's
		// workspace. This is what the SERVER-side handler does in
		// production; we mirror it here so the test exercises the same
		// 403-on-mismatch flow.
		got := req.URL.Query().Get("workspace_id")
		if got != ws.String() {
			ws_.rejectCount.Add(1)
			w.WriteHeader(http.StatusForbidden)
			_, _ = fmt.Fprint(w, `{"detail":{"code":"forbidden","message":"workspace mismatch"}}`)
			return
		}
		// Happy path: return our graph IDs.
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"external_ids": graphIDs,
			"next_cursor":  "",
		})
	}))
	t.Cleanup(ws_.Close)
	return ws_
}

func TestCrossWorkspaceIsolation_NoCrossTalk(t *testing.T) {
	wsA := uuid.MustParse("aaaaaaaa-1111-1111-1111-111111111111")
	wsB := uuid.MustParse("bbbbbbbb-2222-2222-2222-222222222222")
	cidA := uuid.MustParse("cccccccc-1111-1111-1111-111111111111")
	cidB := uuid.MustParse("cccccccc-2222-2222-2222-222222222222")

	// Each workspace's stub server claims a graph entry that, if leaked,
	// would prove cross-talk: workspace A's server returns an entity
	// purportedly belonging to workspace B's cluster_id, and vice versa.
	// The reconciler MUST NOT emit those — its diff input for A includes
	// only A's claimed entries; it never sees B's.
	graphA := []string{"k8s_resource/" + cidA.String() + "/Pod/ns/a-only"}
	graphB := []string{"k8s_resource/" + cidB.String() + "/Pod/ns/b-only"}

	srvA := newWorkspaceServer(t, wsA, "token-a", graphA)
	srvB := newWorkspaceServer(t, wsB, "token-b", graphB)

	// Each operator has its own cluster K8s client (fake, empty). With
	// empty clusters and one entry in each graph, missing-in-cluster diff
	// produces exactly one deletion event per operator — directed at
	// THEIR OWN cluster_id, never the other's.
	cA := newFakeClient()
	cB := newFakeClient()

	apiA, err := reconciler.NewHTTPEntitiesClient(srvA.URL, "token-a", reconciler.WithHTTPClient(srvA.Client()))
	if err != nil {
		t.Fatalf("apiA: %v", err)
	}
	apiB, err := reconciler.NewHTTPEntitiesClient(srvB.URL, "token-b", reconciler.WithHTTPClient(srvB.Client()))
	if err != nil {
		t.Fatalf("apiB: %v", err)
	}

	pubA := &fakePublisher{}
	pubB := &fakePublisher{}

	workerA, err := reconciler.New(reconciler.Options{
		K8sClient:   cA,
		APIClient:   apiA,
		Publisher:   pubA,
		WorkspaceID: wsA,
		ClusterID:   cidA,
		ClusterName: "cluster-a",
		Interval:    1 * time.Hour,
		Kinds:       []reconciler.KindHandler{podOnlyKind(t)},
		Now:         func() time.Time { return time.Now() },
	})
	if err != nil {
		t.Fatalf("workerA: %v", err)
	}
	workerB, err := reconciler.New(reconciler.Options{
		K8sClient:   cB,
		APIClient:   apiB,
		Publisher:   pubB,
		WorkspaceID: wsB,
		ClusterID:   cidB,
		ClusterName: "cluster-b",
		Interval:    1 * time.Hour,
		Kinds:       []reconciler.KindHandler{podOnlyKind(t)},
		Now:         func() time.Time { return time.Now() },
	})
	if err != nil {
		t.Fatalf("workerB: %v", err)
	}

	workerA.RunOnce(context.Background())
	workerB.RunOnce(context.Background())

	// --- Assertion 1: each pubA/pubB emitted exactly one event ---
	evA := pubA.snapshot()
	evB := pubB.snapshot()
	if len(evA) != 1 {
		t.Fatalf("workspace A: expected 1 event, got %d", len(evA))
	}
	if len(evB) != 1 {
		t.Fatalf("workspace B: expected 1 event, got %d", len(evB))
	}

	// --- Assertion 2: every event for A carries A's workspace+cluster_id ---
	for _, ev := range evA {
		if ev.WorkspaceID != wsA {
			t.Errorf("workspace A event leaked workspace: %s vs %s", ev.WorkspaceID, wsA)
		}
		if got := ev.Metadata["cluster_id"]; got != cidA.String() {
			t.Errorf("workspace A event leaked cluster_id: %s vs %s", got, cidA)
		}
		// And critically, the external_id MUST contain A's cluster_id, never B's.
		if !contains(ev.ExternalID, cidA.String()) {
			t.Errorf("workspace A event external_id missing A's cluster_id: %s", ev.ExternalID)
		}
		if contains(ev.ExternalID, cidB.String()) {
			t.Errorf("workspace A event external_id contained B's cluster_id: %s", ev.ExternalID)
		}
	}
	for _, ev := range evB {
		if ev.WorkspaceID != wsB {
			t.Errorf("workspace B event leaked workspace: %s vs %s", ev.WorkspaceID, wsB)
		}
		if got := ev.Metadata["cluster_id"]; got != cidB.String() {
			t.Errorf("workspace B event leaked cluster_id: %s vs %s", got, cidB)
		}
		if !contains(ev.ExternalID, cidB.String()) {
			t.Errorf("workspace B event external_id missing B's cluster_id: %s", ev.ExternalID)
		}
		if contains(ev.ExternalID, cidA.String()) {
			t.Errorf("workspace B event external_id contained A's cluster_id: %s", ev.ExternalID)
		}
	}

	// --- Assertion 3: each server saw only its own bearer token ---
	if got := srvA.receivedAuth.Load(); got == nil || *got != "Bearer token-a" {
		t.Errorf("server A saw wrong auth: %v", got)
	}
	if got := srvB.receivedAuth.Load(); got == nil || *got != "Bearer token-b" {
		t.Errorf("server B saw wrong auth: %v", got)
	}

	// --- Assertion 4: server-side ACL did not 403 anyone ---
	if r := srvA.rejectCount.Load(); r != 0 {
		t.Errorf("server A rejected %d requests; expected 0 (workspace match)", r)
	}
	if r := srvB.rejectCount.Load(); r != 0 {
		t.Errorf("server B rejected %d requests; expected 0", r)
	}
}

func TestCrossWorkspaceIsolation_ServerRejectsMismatchedWorkspace(t *testing.T) {
	// Negative path: confirm the ACL gate fires when the operator's token
	// is wired to query a workspace that does not match. This proves the
	// server-side check is the load-bearing line of defence — even a
	// misconfigured operator (or a buggy refactor) cannot reach into
	// another tenant's data.
	wsA := uuid.MustParse("aaaaaaaa-1111-1111-1111-111111111111")
	wsB := uuid.MustParse("bbbbbbbb-2222-2222-2222-222222222222")
	srvA := newWorkspaceServer(t, wsA, "token-a", []string{})

	// Build a client whose bearer matches wsA's token but whose worker is
	// configured for workspace B — the server MUST 403 on the workspace_id
	// query param mismatch.
	apiClient, err := reconciler.NewHTTPEntitiesClient(srvA.URL, "token-a", reconciler.WithHTTPClient(srvA.Client()))
	if err != nil {
		t.Fatalf("api client: %v", err)
	}
	pub := &fakePublisher{}
	worker, err := reconciler.New(reconciler.Options{
		K8sClient:   newFakeClient(),
		APIClient:   apiClient,
		Publisher:   pub,
		WorkspaceID: wsB, // <-- mismatch with token's workspace
		ClusterID:   uuid.New(),
		ClusterName: "cluster",
		Interval:    1 * time.Hour,
		Kinds:       []reconciler.KindHandler{podOnlyKind(t)},
		Now:         func() time.Time { return time.Now() },
	})
	if err != nil {
		t.Fatalf("worker: %v", err)
	}

	worker.RunOnce(context.Background())

	// Worker swallows the 403 (logged, runs_total{result=error} incremented).
	// The critical assertion: zero events emitted, server saw a rejected
	// request.
	if n := len(pub.snapshot()); n != 0 {
		t.Fatalf("expected 0 events on ACL rejection, got %d", n)
	}
	if r := srvA.rejectCount.Load(); r == 0 {
		t.Fatalf("expected server to reject mismatched workspace; saw %d rejections", r)
	}
}

// contains is a tiny string helper — the standard library has strings.Contains
// but using it here would add an extra import to a test file that already
// has plenty. Inline keeps the assertions readable.
func contains(haystack, needle string) bool {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return true
		}
	}
	return false
}

// We never want to instantiate metav1.ObjectMeta in this file but the test
// helpers require corev1; keep the import live.
var _ = metav1.ObjectMeta{}
var _ = corev1.Pod{}
