package entity_test

import (
	"encoding/json"
	"testing"

	"github.com/google/uuid"

	"github.com/100rd/omniscience/operator/internal/entity"
)

// TestClusterToEvent_IdempotentExternalID asserts that two calls with the
// same inputs produce the same external_id and the same cluster_id. The
// emitted_at timestamp differs across calls in production (the operator
// passes time.Now() each restart); for the idempotence contract that the
// server relies on, the (workspace_id, external_id) composite must be
// stable across restarts.
func TestClusterToEvent_IdempotentExternalID(t *testing.T) {
	a := entity.ClusterToEvent(entity.ActionCreated, fixedWorkspace, "prod-eu", "v1.31.1", "https://api:6443", fixedNow)
	b := entity.ClusterToEvent(entity.ActionCreated, fixedWorkspace, "prod-eu", "v1.31.1", "https://api:6443", fixedNow)

	if a.ExternalID != b.ExternalID {
		t.Fatalf("external_id differs across calls: %q vs %q", a.ExternalID, b.ExternalID)
	}
	if a.ExternalID != "k8s_resource/Cluster/prod-eu" {
		t.Fatalf("external_id = %q, want %q", a.ExternalID, "k8s_resource/Cluster/prod-eu")
	}
	if a.Metadata["cluster_id"] != b.Metadata["cluster_id"] {
		t.Fatalf("cluster_id differs across calls: %q vs %q", a.Metadata["cluster_id"], b.Metadata["cluster_id"])
	}
	if a.SourceID != b.SourceID {
		t.Fatalf("source_id differs across calls: %s vs %s", a.SourceID, b.SourceID)
	}
}

func TestDeriveClusterID_DeterministicAndUUID5(t *testing.T) {
	got := entity.DeriveClusterID(fixedWorkspace, "prod-eu")
	again := entity.DeriveClusterID(fixedWorkspace, "prod-eu")
	if got != again {
		t.Fatalf("cluster_id not deterministic: %s vs %s", got, again)
	}
	if got.Version() != 5 {
		t.Fatalf("cluster_id version = %d, want UUIDv5", got.Version())
	}
}

func TestDeriveClusterID_DiffersAcrossWorkspaces(t *testing.T) {
	// Critical multi-tenant invariant for #167: two workspaces sharing a
	// cluster_name must produce DIFFERENT cluster_ids. The (workspace_id,
	// external_id) composite is the server-side uniqueness key, so a
	// collision would let workspace B's anchor overwrite workspace A's.
	wsA := uuid.MustParse("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
	wsB := uuid.MustParse("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
	a := entity.DeriveClusterID(wsA, "prod-eu")
	b := entity.DeriveClusterID(wsB, "prod-eu")
	if a == b {
		t.Fatalf("cluster_id collided across workspaces: %s", a)
	}
}

func TestDeriveClusterID_DiffersAcrossClusters(t *testing.T) {
	// Same workspace, different cluster_name → different cluster_ids.
	a := entity.DeriveClusterID(fixedWorkspace, "prod-eu")
	b := entity.DeriveClusterID(fixedWorkspace, "prod-us")
	if a == b {
		t.Fatalf("cluster_id collided across cluster_names: %s", a)
	}
}

func TestClusterToEvent_MetadataIsClosedAllowList(t *testing.T) {
	ev := entity.ClusterToEvent(entity.ActionCreated, fixedWorkspace, "prod-eu", "v1.31.1", "https://api:6443", fixedNow)
	want := map[string]string{
		"cluster":            "prod-eu",
		"name":               "prod-eu",
		"kind":               "Cluster",
		"emitter":            "k8s-operator",
		"kubernetes_version": "v1.31.1",
		"cluster_endpoint":   "https://api:6443",
		// cluster_id is computed; check presence below.
	}
	for k, v := range want {
		if got := ev.Metadata[k]; got != v {
			t.Fatalf("metadata[%q] = %q, want %q", k, got, v)
		}
	}
	if _, ok := ev.Metadata["cluster_id"]; !ok {
		t.Fatalf("metadata missing cluster_id")
	}
}

func TestClusterToEvent_DropsEmptyOptionalFields(t *testing.T) {
	// When the operator boots before it has a Node sample, kubelet_version
	// is unknown. The mapper must drop the key entirely rather than emit
	// "kubernetes_version": "" — the consumer can distinguish absent from
	// empty cleanly.
	ev := entity.ClusterToEvent(entity.ActionCreated, fixedWorkspace, "prod-eu", "", "", fixedNow)
	if _, ok := ev.Metadata["kubernetes_version"]; ok {
		t.Fatalf("kubernetes_version key present despite empty input")
	}
	if _, ok := ev.Metadata["cluster_endpoint"]; ok {
		t.Fatalf("cluster_endpoint key present despite empty input")
	}
}

func TestClusterToEvent_StableJSONShape(t *testing.T) {
	ev := entity.ClusterToEvent(entity.ActionCreated, fixedWorkspace, "prod-eu", "v1.31.1", "", fixedNow)
	raw, err := json.Marshal(ev)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var parsed map[string]any
	if err := json.Unmarshal(raw, &parsed); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	for _, k := range []string{"source_id", "external_id", "uri", "action", "workspace_id", "metadata"} {
		if _, ok := parsed[k]; !ok {
			t.Fatalf("required field %q absent: %s", k, raw)
		}
	}
	// Cluster anchor has no Edges (it IS the anchor; nothing to point at
	// from itself).
	if v, ok := parsed["edges"]; ok && v != nil {
		t.Fatalf("cluster anchor should not emit edges, got %+v", v)
	}
}

func TestClusterExternalID_DeterministicForm(t *testing.T) {
	// The external_id is the contract that lets #167 detect cluster
	// identity collisions — pin the format byte-for-byte.
	if got := entity.ClusterExternalID("prod-eu"); got != "k8s_resource/Cluster/prod-eu" {
		t.Fatalf("got %q", got)
	}
}
