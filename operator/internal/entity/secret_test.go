package entity_test

import (
	"encoding/base64"
	"encoding/json"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/Omniscience/operator/internal/entity"
)

// hostileSecret carries values the test will substring-search for in the
// emitted JSON. The string-and-bytes form lets us catch both the raw byte
// path and the base64-encoded path that JSON marshalling produces for
// []byte fields.
func hostileSecret() *corev1.Secret {
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "team-a",
			Name:      "stripe-prod-key",
			// Hostile labels & annotations — must NOT appear in metadata.
			Labels:      map[string]string{"adversarial.example.com/spy": "yes"},
			Annotations: map[string]string{"omniscience.io/index-data": "true"}, // even the opt-in must be ignored
		},
		Type: corev1.SecretTypeOpaque,
		Data: map[string][]byte{
			"db_password": []byte("CANARY-PWD-XYZZY-1"),
			"api_token":   []byte("CANARY-TOK-XYZZY-2"),
		},
		StringData: map[string]string{
			"client_secret": "CANARY-STR-XYZZY-3",
		},
	}
}

// TestSecretToEvent_NeverLeaksValues is the structural test for the hard
// rule. We marshal the emitted Event to JSON and assert that none of the
// canary substrings appear ANYWHERE in the payload — neither raw, nor as a
// JSON-escaped string, nor as a base64-encoded blob (the form that []byte
// would take in encoding/json).
func TestSecretToEvent_NeverLeaksValues(t *testing.T) {
	s := hostileSecret()
	ev := entity.SecretToEvent(s, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	raw, err := json.Marshal(ev)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	payload := string(raw)
	canaries := []string{
		"CANARY-PWD-XYZZY-1",
		"CANARY-TOK-XYZZY-2",
		"CANARY-STR-XYZZY-3",
		// Base64-encoded forms, in case a future regression smuggles []byte
		// values through.
		base64.StdEncoding.EncodeToString([]byte("CANARY-PWD-XYZZY-1")),
		base64.StdEncoding.EncodeToString([]byte("CANARY-TOK-XYZZY-2")),
	}
	for _, c := range canaries {
		if strings.Contains(payload, c) {
			t.Fatalf("SECRET VALUE LEAK: canary %q appears in event payload: %s", c, payload)
		}
	}
	// Hostile labels / annotations also must not leak.
	if strings.Contains(payload, "adversarial.example.com/spy") {
		t.Fatalf("hostile label leaked into Secret payload: %s", payload)
	}
}

func TestSecretToEvent_OptInAnnotationIsIgnored(t *testing.T) {
	// The Secret mapper has NO opt-in. Even with the same annotation
	// ConfigMap honours, Secret values stay dropped.
	s := hostileSecret()
	ev := entity.SecretToEvent(s, entity.ActionUpdated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if _, ok := ev.Metadata["data_values_json"]; ok {
		t.Fatalf("Secret must NOT honour the index-data annotation: data_values_json is present")
	}
	if _, ok := ev.Metadata["indexed"]; ok {
		t.Fatalf("Secret must NOT carry the 'indexed' field — it's a ConfigMap-only marker")
	}
}

func TestSecretToEvent_AllowListIsClosed(t *testing.T) {
	s := hostileSecret()
	ev := entity.SecretToEvent(s, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	allowed := map[string]struct{}{
		"cluster": {}, "cluster_id": {}, "namespace": {}, "name": {}, "kind": {}, "emitter": {},
		"secret_type": {}, "data_keys": {}, "data_count": {},
	}
	for k := range ev.Metadata {
		if _, ok := allowed[k]; !ok {
			t.Fatalf("Secret metadata leaked unexpected key %q", k)
		}
	}
	if ev.Metadata["kind"] != "Secret" {
		t.Fatalf("kind = %q", ev.Metadata["kind"])
	}
	if ev.Metadata["secret_type"] != "Opaque" {
		t.Fatalf("secret_type = %q", ev.Metadata["secret_type"])
	}
	// data_keys must be the sorted union of Data and StringData keys.
	if ev.Metadata["data_keys"] != "api_token,client_secret,db_password" {
		t.Fatalf("data_keys = %q (sorted union expected)", ev.Metadata["data_keys"])
	}
	if ev.Metadata["data_count"] != "3" {
		t.Fatalf("data_count = %q", ev.Metadata["data_count"])
	}
}

func TestSecretToEvent_TLSTypeMetadata(t *testing.T) {
	s := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "tls-cert"},
		Type:       corev1.SecretTypeTLS,
		Data: map[string][]byte{
			corev1.TLSCertKey:       []byte("CANARY-CERT-PEM"),
			corev1.TLSPrivateKeyKey: []byte("CANARY-KEY-PEM"),
		},
	}
	ev := entity.SecretToEvent(s, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if ev.Metadata["secret_type"] != "kubernetes.io/tls" {
		t.Fatalf("secret_type = %q", ev.Metadata["secret_type"])
	}
	if ev.Metadata["data_keys"] != corev1.TLSCertKey+","+corev1.TLSPrivateKeyKey {
		// "tls.crt,tls.key" — sorted lexicographically.
		t.Fatalf("data_keys = %q", ev.Metadata["data_keys"])
	}
	// Last belt-and-braces leak check on the TLS variant.
	raw, err := json.Marshal(ev)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	for _, c := range []string{"CANARY-CERT-PEM", "CANARY-KEY-PEM",
		base64.StdEncoding.EncodeToString([]byte("CANARY-CERT-PEM")),
		base64.StdEncoding.EncodeToString([]byte("CANARY-KEY-PEM")),
	} {
		if strings.Contains(string(raw), c) {
			t.Fatalf("TLS Secret value leaked: %q in %s", c, raw)
		}
	}
}

func TestSecretToEvent_NamespacelessFallsBack(t *testing.T) {
	s := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: "rogue"},
		Type:       corev1.SecretTypeOpaque,
	}
	ev := entity.SecretToEvent(s, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/Secret/default/rogue" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
}
