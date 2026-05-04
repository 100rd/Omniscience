// persistentvolumeclaim_mapper_test.go: pure unit tests for PersistentVolumeClaimToEvent (#208).
package entity_test

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/omniscience/operator/internal/entity"
)

var (
	pvcTestWorkspace   = uuid.MustParse("11111111-2222-3333-4444-555555555555")
	pvcTestClusterID   = uuid.MustParse("99999999-8888-7777-6666-555555555555")
	pvcTestClusterName = "prod-eu"
	pvcTestNow         = time.Date(2026, 5, 4, 12, 0, 0, 0, time.UTC)
)

func pvcQty(s string) resource.Quantity { return resource.MustParse(s) }

func strPtr(s string) *string { return &s }

func volModePtr(m corev1.PersistentVolumeMode) *corev1.PersistentVolumeMode { return &m }

// ─── Envelope ─────────────────────────────────────────────────────────────

func TestPVCToEvent_EnvelopeStampsWorkspaceFromConfig(t *testing.T) {
	pvc := &corev1.PersistentVolumeClaim{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "team-a",
			Name:      "data",
			Labels: map[string]string{
				"omniscience.io/workspace": "evil-ws",
				"team":                     "platform",
			},
			Annotations: map[string]string{
				"omniscience.io/index-data": "true",
			},
		},
	}
	ev := entity.PersistentVolumeClaimToEvent(pvc, entity.ActionUpdated, pvcTestWorkspace, pvcTestClusterID, pvcTestClusterName, pvcTestNow)

	if ev.WorkspaceID != pvcTestWorkspace {
		t.Fatalf("workspace_id = %s, want %s — adversarial label honoured!", ev.WorkspaceID, pvcTestWorkspace)
	}
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/PersistentVolumeClaim/team-a/data" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.URI != "kube://prod-eu/team-a/PersistentVolumeClaim/data" {
		t.Fatalf("uri = %q", ev.URI)
	}
	if !ev.EmittedAt.Equal(pvcTestNow) {
		t.Fatalf("emitted_at = %s", ev.EmittedAt)
	}
}

// ACL invariant: only the closed allow-list of metadata keys is emitted.
func TestPVCToEvent_ACL_NoUserLabelsOrAnnotationsLeak(t *testing.T) {
	pvc := &corev1.PersistentVolumeClaim{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "team-a",
			Name:      "data",
			Labels: map[string]string{
				"team":          "platform",
				"cost-center":   "eng-42",
				"app.k8s.io/of": "checkout",
			},
			Annotations: map[string]string{
				"description": "team-a data volume",
				"owner":       "alice@example.com",
			},
		},
	}
	ev := entity.PersistentVolumeClaimToEvent(pvc, entity.ActionUpdated, pvcTestWorkspace, pvcTestClusterID, pvcTestClusterName, pvcTestNow)

	allowed := map[string]struct{}{
		"cluster": {}, "cluster_id": {}, "namespace": {}, "name": {}, "kind": {}, "emitter": {},
		"access_modes_json": {}, "storage_class_name": {}, "volume_name": {}, "volume_mode": {},
		"requests_storage": {}, "limits_storage": {}, "status_phase": {},
	}
	for k := range ev.Metadata {
		if _, ok := allowed[k]; !ok {
			t.Fatalf("metadata leaked key %q (value=%q) — ACL allow-list violated", k, ev.Metadata[k])
		}
	}
	for forbidden := range pvc.Labels {
		if v, ok := ev.Metadata[forbidden]; ok {
			t.Fatalf("user label %q leaked into metadata as %q", forbidden, v)
		}
	}
	for forbidden := range pvc.Annotations {
		if v, ok := ev.Metadata[forbidden]; ok {
			t.Fatalf("user annotation %q leaked into metadata as %q", forbidden, v)
		}
	}
}

// ─── Fixture 1: unbound PVC (Pending), nothing set yet ────────────────────

func TestPVCToEvent_UnboundPending(t *testing.T) {
	pvc := &corev1.PersistentVolumeClaim{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "data"},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{corev1.ResourceStorage: pvcQty("10Gi")},
			},
		},
		Status: corev1.PersistentVolumeClaimStatus{Phase: corev1.ClaimPending},
	}
	ev := entity.PersistentVolumeClaimToEvent(pvc, entity.ActionCreated, pvcTestWorkspace, pvcTestClusterID, pvcTestClusterName, pvcTestNow)

	if ev.Metadata["status_phase"] != "Pending" {
		t.Fatalf("status_phase = %q", ev.Metadata["status_phase"])
	}
	if ev.Metadata["volume_name"] != "" {
		t.Fatalf("volume_name = %q (should be empty when unbound)", ev.Metadata["volume_name"])
	}
	if ev.Metadata["requests_storage"] != "10Gi" {
		t.Fatalf("requests_storage = %q", ev.Metadata["requests_storage"])
	}
	var modes []string
	if err := json.Unmarshal([]byte(ev.Metadata["access_modes_json"]), &modes); err != nil {
		t.Fatalf("access_modes_json: %v", err)
	}
	if len(modes) != 1 || modes[0] != "ReadWriteOnce" {
		t.Fatalf("access_modes = %#v", modes)
	}
}

// ─── Fixture 2: bound PVC with storage class, volume name, multiple access modes ──

func TestPVCToEvent_BoundWithStorageClass(t *testing.T) {
	pvc := &corev1.PersistentVolumeClaim{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "data"},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes:      []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce, corev1.ReadOnlyMany},
			StorageClassName: strPtr("fast-ssd"),
			VolumeName:       "pv-12345",
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{corev1.ResourceStorage: pvcQty("100Gi")},
			},
		},
		Status: corev1.PersistentVolumeClaimStatus{Phase: corev1.ClaimBound},
	}
	ev := entity.PersistentVolumeClaimToEvent(pvc, entity.ActionUpdated, pvcTestWorkspace, pvcTestClusterID, pvcTestClusterName, pvcTestNow)

	if ev.Metadata["status_phase"] != "Bound" {
		t.Fatalf("status_phase = %q", ev.Metadata["status_phase"])
	}
	if ev.Metadata["volume_name"] != "pv-12345" {
		t.Fatalf("volume_name = %q", ev.Metadata["volume_name"])
	}
	if ev.Metadata["storage_class_name"] != "fast-ssd" {
		t.Fatalf("storage_class_name = %q", ev.Metadata["storage_class_name"])
	}
	var modes []string
	if err := json.Unmarshal([]byte(ev.Metadata["access_modes_json"]), &modes); err != nil {
		t.Fatalf("access_modes_json: %v", err)
	}
	if len(modes) != 2 || modes[0] != "ReadOnlyMany" || modes[1] != "ReadWriteOnce" {
		t.Fatalf("access_modes = %#v (must be sorted)", modes)
	}
}

// ─── Fixture 3: with limits, Block volume mode ────────────────────────────

func TestPVCToEvent_WithLimitsAndBlockVolumeMode(t *testing.T) {
	pvc := &corev1.PersistentVolumeClaim{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "raw-block"},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
			VolumeMode:  volModePtr(corev1.PersistentVolumeBlock),
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{corev1.ResourceStorage: pvcQty("50Gi")},
				Limits:   corev1.ResourceList{corev1.ResourceStorage: pvcQty("100Gi")},
			},
		},
	}
	ev := entity.PersistentVolumeClaimToEvent(pvc, entity.ActionUpdated, pvcTestWorkspace, pvcTestClusterID, pvcTestClusterName, pvcTestNow)
	if ev.Metadata["volume_mode"] != "Block" {
		t.Fatalf("volume_mode = %q", ev.Metadata["volume_mode"])
	}
	if ev.Metadata["limits_storage"] != "100Gi" {
		t.Fatalf("limits_storage = %q", ev.Metadata["limits_storage"])
	}
	if ev.Metadata["requests_storage"] != "50Gi" {
		t.Fatalf("requests_storage = %q", ev.Metadata["requests_storage"])
	}
}

// ─── Fixture 4: nil storage class pointer (uses cluster default) ──────────

func TestPVCToEvent_NilStorageClass(t *testing.T) {
	pvc := &corev1.PersistentVolumeClaim{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "default-sc"},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
			// StorageClassName explicitly nil — falls back to cluster default.
		},
	}
	ev := entity.PersistentVolumeClaimToEvent(pvc, entity.ActionUpdated, pvcTestWorkspace, pvcTestClusterID, pvcTestClusterName, pvcTestNow)
	if ev.Metadata["storage_class_name"] != "" {
		t.Fatalf("storage_class_name = %q, want empty for nil pointer", ev.Metadata["storage_class_name"])
	}
	if ev.Metadata["volume_mode"] != "" {
		t.Fatalf("volume_mode = %q, want empty for nil pointer", ev.Metadata["volume_mode"])
	}
}

// ─── Determinism ──────────────────────────────────────────────────────────

func TestPVCToEvent_DeterministicAccessModesJSON(t *testing.T) {
	build := func() *corev1.PersistentVolumeClaim {
		return &corev1.PersistentVolumeClaim{
			ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "x"},
			Spec: corev1.PersistentVolumeClaimSpec{
				// Reverse-alphabetical — mapper must sort.
				AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteMany, corev1.ReadOnlyMany, corev1.ReadWriteOnce},
			},
		}
	}
	a := entity.PersistentVolumeClaimToEvent(build(), entity.ActionUpdated, pvcTestWorkspace, pvcTestClusterID, pvcTestClusterName, pvcTestNow)
	b := entity.PersistentVolumeClaimToEvent(build(), entity.ActionUpdated, pvcTestWorkspace, pvcTestClusterID, pvcTestClusterName, pvcTestNow)
	if a.Metadata["access_modes_json"] != b.Metadata["access_modes_json"] {
		t.Fatalf("non-deterministic access_modes_json:\nA: %s\nB: %s", a.Metadata["access_modes_json"], b.Metadata["access_modes_json"])
	}
	if a.Metadata["access_modes_json"] != `["ReadOnlyMany","ReadWriteMany","ReadWriteOnce"]` {
		t.Fatalf("access_modes_json not in expected sorted form: %s", a.Metadata["access_modes_json"])
	}
}

// ─── Default namespace fallback ───────────────────────────────────────────

func TestPVCToEvent_EmptyNamespaceFallsBackToDefault(t *testing.T) {
	pvc := &corev1.PersistentVolumeClaim{}
	pvc.Name = "x"
	ev := entity.PersistentVolumeClaimToEvent(pvc, entity.ActionUpdated, pvcTestWorkspace, pvcTestClusterID, pvcTestClusterName, pvcTestNow)
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/PersistentVolumeClaim/default/x" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.Metadata["namespace"] != "default" {
		t.Fatalf("namespace = %q", ev.Metadata["namespace"])
	}
}
