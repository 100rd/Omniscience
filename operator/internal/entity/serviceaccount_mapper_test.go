// serviceaccount_mapper_test.go: pure unit tests for ServiceAccountToEvent (#206).
package entity_test

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/Omniscience/operator/internal/entity"
)

var (
	saTestWorkspace   = uuid.MustParse("11111111-2222-3333-4444-555555555555")
	saTestClusterID   = uuid.MustParse("99999999-8888-7777-6666-555555555555")
	saTestClusterName = "prod-eu"
	saTestNow         = time.Date(2026, 5, 4, 12, 0, 0, 0, time.UTC)
)

func boolPtr(b bool) *bool { return &b }

// ─── Envelope ─────────────────────────────────────────────────────────────

func TestServiceAccountToEvent_EnvelopeStampsWorkspaceFromConfig(t *testing.T) {
	sa := &corev1.ServiceAccount{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "team-a",
			Name:      "build-bot",
			// Adversarial labels — ACL invariant: tenancy from operator
			// config, not from cluster state.
			Labels: map[string]string{
				"omniscience.io/workspace": "evil-ws",
				"team":                     "platform",
			},
			Annotations: map[string]string{
				"omniscience.io/index-data": "true",
			},
		},
	}
	ev := entity.ServiceAccountToEvent(sa, entity.ActionUpdated, saTestWorkspace, saTestClusterID, saTestClusterName, saTestNow)

	if ev.WorkspaceID != saTestWorkspace {
		t.Fatalf("workspace_id = %s, want %s — adversarial label honoured!", ev.WorkspaceID, saTestWorkspace)
	}
	if ev.SourceType != entity.SourceType {
		t.Fatalf("source_type = %q", ev.SourceType)
	}
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/ServiceAccount/team-a/build-bot" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.URI != "kube://prod-eu/team-a/ServiceAccount/build-bot" {
		t.Fatalf("uri = %q", ev.URI)
	}
	if !ev.EmittedAt.Equal(saTestNow) {
		t.Fatalf("emitted_at = %s", ev.EmittedAt)
	}
	if ev.Action != entity.ActionUpdated {
		t.Fatalf("action = %q", ev.Action)
	}
}

// ACL invariant: only the closed allow-list of metadata keys is emitted.
func TestServiceAccountToEvent_ACL_NoUserLabelsOrAnnotationsLeak(t *testing.T) {
	sa := &corev1.ServiceAccount{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "team-a",
			Name:      "build-bot",
			Labels: map[string]string{
				"team":          "platform",
				"cost-center":   "eng-42",
				"app.k8s.io/of": "checkout",
			},
			Annotations: map[string]string{
				"description": "team-a build bot",
				"owner":       "alice@example.com",
			},
		},
	}
	ev := entity.ServiceAccountToEvent(sa, entity.ActionUpdated, saTestWorkspace, saTestClusterID, saTestClusterName, saTestNow)

	allowed := map[string]struct{}{
		"cluster": {}, "cluster_id": {}, "namespace": {}, "name": {}, "kind": {}, "emitter": {},
		"secrets_count": {}, "secrets_json": {},
		"image_pull_secrets_count": {}, "image_pull_secrets_json": {},
		"automount_service_account_token": {},
	}
	for k := range ev.Metadata {
		if _, ok := allowed[k]; !ok {
			t.Fatalf("metadata leaked key %q (value=%q) — ACL allow-list violated", k, ev.Metadata[k])
		}
	}
	for forbidden := range sa.Labels {
		if v, ok := ev.Metadata[forbidden]; ok {
			t.Fatalf("user label %q leaked into metadata as %q", forbidden, v)
		}
	}
	for forbidden := range sa.Annotations {
		if v, ok := ev.Metadata[forbidden]; ok {
			t.Fatalf("user annotation %q leaked into metadata as %q", forbidden, v)
		}
	}
}

// ─── Fixture 1: default SA — no secrets, no image pull secrets, automount nil ──

func TestServiceAccountToEvent_DefaultSA(t *testing.T) {
	sa := &corev1.ServiceAccount{
		ObjectMeta: metav1.ObjectMeta{Namespace: "default", Name: "default"},
	}
	ev := entity.ServiceAccountToEvent(sa, entity.ActionUpdated, saTestWorkspace, saTestClusterID, saTestClusterName, saTestNow)

	if ev.Metadata["secrets_count"] != "0" {
		t.Fatalf("secrets_count = %q", ev.Metadata["secrets_count"])
	}
	if _, present := ev.Metadata["secrets_json"]; present {
		t.Fatalf("secrets_json must be absent when .Secrets is empty")
	}
	if ev.Metadata["image_pull_secrets_count"] != "0" {
		t.Fatalf("image_pull_secrets_count = %q", ev.Metadata["image_pull_secrets_count"])
	}
	if _, present := ev.Metadata["image_pull_secrets_json"]; present {
		t.Fatalf("image_pull_secrets_json must be absent when .ImagePullSecrets is empty")
	}
	if ev.Metadata["automount_service_account_token"] != "" {
		t.Fatalf("automount = %q, want empty for nil pointer", ev.Metadata["automount_service_account_token"])
	}
}

// ─── Fixture 2: with secrets (sorted JSON) ────────────────────────────────

func TestServiceAccountToEvent_WithSecrets(t *testing.T) {
	sa := &corev1.ServiceAccount{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "build-bot"},
		Secrets: []corev1.ObjectReference{
			// Intentionally reverse-alphabetical — mapper must sort.
			{Name: "github-token", UID: "uid-1"},
			{Name: "aws-creds", UID: "uid-2"},
			{Name: "build-bot-token-abc", UID: "uid-3"},
		},
	}
	ev := entity.ServiceAccountToEvent(sa, entity.ActionUpdated, saTestWorkspace, saTestClusterID, saTestClusterName, saTestNow)

	if ev.Metadata["secrets_count"] != "3" {
		t.Fatalf("secrets_count = %q", ev.Metadata["secrets_count"])
	}
	var got []string
	if err := json.Unmarshal([]byte(ev.Metadata["secrets_json"]), &got); err != nil {
		t.Fatalf("secrets_json not valid JSON: %v", err)
	}
	if len(got) != 3 || got[0] != "aws-creds" || got[1] != "build-bot-token-abc" || got[2] != "github-token" {
		t.Fatalf("secrets_json = %#v (must be sorted by name only — no UID/ResourceVersion)", got)
	}
}

// ─── Fixture 3: with image pull secrets ───────────────────────────────────

func TestServiceAccountToEvent_WithImagePullSecrets(t *testing.T) {
	sa := &corev1.ServiceAccount{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "deploy-bot"},
		ImagePullSecrets: []corev1.LocalObjectReference{
			{Name: "ghcr-creds"},
			{Name: "dockerhub-creds"},
		},
	}
	ev := entity.ServiceAccountToEvent(sa, entity.ActionCreated, saTestWorkspace, saTestClusterID, saTestClusterName, saTestNow)

	if ev.Metadata["image_pull_secrets_count"] != "2" {
		t.Fatalf("image_pull_secrets_count = %q", ev.Metadata["image_pull_secrets_count"])
	}
	var got []string
	if err := json.Unmarshal([]byte(ev.Metadata["image_pull_secrets_json"]), &got); err != nil {
		t.Fatalf("image_pull_secrets_json not valid JSON: %v", err)
	}
	if len(got) != 2 || got[0] != "dockerhub-creds" || got[1] != "ghcr-creds" {
		t.Fatalf("image_pull_secrets_json = %#v (must be sorted)", got)
	}
}

// ─── Fixture 4: automount true / false ────────────────────────────────────

func TestServiceAccountToEvent_AutomountTrue(t *testing.T) {
	sa := &corev1.ServiceAccount{
		ObjectMeta:                   metav1.ObjectMeta{Namespace: "team-a", Name: "x"},
		AutomountServiceAccountToken: boolPtr(true),
	}
	ev := entity.ServiceAccountToEvent(sa, entity.ActionUpdated, saTestWorkspace, saTestClusterID, saTestClusterName, saTestNow)
	if ev.Metadata["automount_service_account_token"] != "true" {
		t.Fatalf("automount = %q", ev.Metadata["automount_service_account_token"])
	}
}

func TestServiceAccountToEvent_AutomountFalse(t *testing.T) {
	sa := &corev1.ServiceAccount{
		ObjectMeta:                   metav1.ObjectMeta{Namespace: "team-a", Name: "x"},
		AutomountServiceAccountToken: boolPtr(false),
	}
	ev := entity.ServiceAccountToEvent(sa, entity.ActionUpdated, saTestWorkspace, saTestClusterID, saTestClusterName, saTestNow)
	if ev.Metadata["automount_service_account_token"] != "false" {
		t.Fatalf("automount = %q", ev.Metadata["automount_service_account_token"])
	}
}

// ─── Determinism: two mappings of the same input produce identical bytes ──

func TestServiceAccountToEvent_DeterministicJSON(t *testing.T) {
	build := func() *corev1.ServiceAccount {
		return &corev1.ServiceAccount{
			ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "x"},
			Secrets: []corev1.ObjectReference{
				{Name: "z-secret"}, {Name: "a-secret"}, {Name: "m-secret"},
			},
			ImagePullSecrets: []corev1.LocalObjectReference{
				{Name: "z-pull"}, {Name: "a-pull"},
			},
		}
	}
	a := entity.ServiceAccountToEvent(build(), entity.ActionUpdated, saTestWorkspace, saTestClusterID, saTestClusterName, saTestNow)
	b := entity.ServiceAccountToEvent(build(), entity.ActionUpdated, saTestWorkspace, saTestClusterID, saTestClusterName, saTestNow)

	if a.Metadata["secrets_json"] != b.Metadata["secrets_json"] {
		t.Fatalf("non-deterministic secrets_json:\nA: %s\nB: %s", a.Metadata["secrets_json"], b.Metadata["secrets_json"])
	}
	if a.Metadata["image_pull_secrets_json"] != b.Metadata["image_pull_secrets_json"] {
		t.Fatalf("non-deterministic image_pull_secrets_json:\nA: %s\nB: %s", a.Metadata["image_pull_secrets_json"], b.Metadata["image_pull_secrets_json"])
	}
	if a.Metadata["secrets_json"] != `["a-secret","m-secret","z-secret"]` {
		t.Fatalf("secrets_json not in expected sorted form: %s", a.Metadata["secrets_json"])
	}
}

// ─── Default namespace fallback ───────────────────────────────────────────

func TestServiceAccountToEvent_EmptyNamespaceFallsBackToDefault(t *testing.T) {
	sa := &corev1.ServiceAccount{}
	sa.Name = "x"
	ev := entity.ServiceAccountToEvent(sa, entity.ActionUpdated, saTestWorkspace, saTestClusterID, saTestClusterName, saTestNow)
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/ServiceAccount/default/x" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.Metadata["namespace"] != "default" {
		t.Fatalf("namespace = %q", ev.Metadata["namespace"])
	}
}

// ─── Critical security check: Secret VALUES must NEVER appear in metadata ──

func TestServiceAccountToEvent_SecretValuesNeverLeak(t *testing.T) {
	// ObjectReference doesn't carry data, but a future type-change could
	// add fields. This belt-and-braces check encodes the invariant.
	sa := &corev1.ServiceAccount{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "build-bot"},
		Secrets: []corev1.ObjectReference{
			{Name: "leaks-via-uid", UID: "secret-uid-do-not-leak"},
			{Name: "leaks-via-rv", ResourceVersion: "secret-rv-do-not-leak"},
		},
	}
	ev := entity.ServiceAccountToEvent(sa, entity.ActionUpdated, saTestWorkspace, saTestClusterID, saTestClusterName, saTestNow)
	for _, v := range ev.Metadata {
		if v == "secret-uid-do-not-leak" || v == "secret-rv-do-not-leak" {
			t.Fatalf("Secret reference UID/ResourceVersion leaked into metadata; emit names only")
		}
	}
	// Names are fine to emit.
	got := ev.Metadata["secrets_json"]
	if got == "" {
		t.Fatalf("secrets_json missing; expected names")
	}
}
