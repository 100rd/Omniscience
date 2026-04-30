package entity_test

import (
	"encoding/json"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/omniscience/operator/internal/entity"
)

// hostileConfigMap returns a ConfigMap that *would* leak if the mapper
// failed: distinctive value bytes like "REDACTED-ME" so a substring-search
// catches any regression.
func hostileConfigMap(opt bool) *corev1.ConfigMap {
	cm := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "team-a",
			Name:      "settings",
			Labels:    map[string]string{"adversarial.example.com/spy": "no"},
		},
		Data: map[string]string{
			"db_password":   "REDACTED-ME-VAL-1",
			"api_token":     "REDACTED-ME-VAL-2",
			"feature_flags": "REDACTED-ME-VAL-3",
		},
		BinaryData: map[string][]byte{
			"cert.der": []byte("REDACTED-ME-BIN-1"),
		},
	}
	if opt {
		cm.Annotations = map[string]string{entity.IndexDataAnnotation: "true"}
	}
	return cm
}

func TestConfigMapToEvent_DefaultDropsValues(t *testing.T) {
	cm := hostileConfigMap(false)
	ev := entity.ConfigMapToEvent(cm, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)

	raw, err := json.Marshal(ev)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	for _, leakSubstr := range []string{
		"REDACTED-ME-VAL-1", "REDACTED-ME-VAL-2", "REDACTED-ME-VAL-3", "REDACTED-ME-BIN-1",
	} {
		if strings.Contains(string(raw), leakSubstr) {
			t.Fatalf("VALUE LEAK: %q appears in default-mode payload: %s", leakSubstr, raw)
		}
	}
	if ev.Metadata["indexed"] != "false" {
		t.Fatalf("indexed = %q, want \"false\"", ev.Metadata["indexed"])
	}
	if ev.Metadata["data_keys"] != "api_token,cert.der,db_password,feature_flags" {
		t.Fatalf("data_keys = %q (want sorted, comma-joined)", ev.Metadata["data_keys"])
	}
	if ev.Metadata["data_count"] != "4" {
		t.Fatalf("data_count = %q", ev.Metadata["data_count"])
	}
	if _, ok := ev.Metadata["data_values_json"]; ok {
		t.Fatalf("data_values_json must NOT appear when annotation is absent")
	}
}

func TestConfigMapToEvent_AnnotationOptInEmitsValuesButNotBinary(t *testing.T) {
	cm := hostileConfigMap(true)
	ev := entity.ConfigMapToEvent(cm, entity.ActionUpdated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	if ev.Metadata["indexed"] != "true" {
		t.Fatalf("indexed = %q, want \"true\"", ev.Metadata["indexed"])
	}
	raw, err := json.Marshal(ev)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	// Textual values must be emitted (opt-in is the contract).
	for _, want := range []string{"REDACTED-ME-VAL-1", "REDACTED-ME-VAL-2", "REDACTED-ME-VAL-3"} {
		if !strings.Contains(string(raw), want) {
			t.Fatalf("opt-in textual value %q missing", want)
		}
	}
	// Binary values must NOT be emitted even with opt-in.
	if strings.Contains(string(raw), "REDACTED-ME-BIN-1") {
		t.Fatalf("BINARY VALUE LEAK: cert.der content appears in opt-in payload (should NEVER happen): %s", raw)
	}
}

func TestConfigMapToEvent_AnyOtherAnnotationValueDoesNotOptIn(t *testing.T) {
	for _, lookalike := range []string{"True", "1", "yes", "TRUE", " true "} {
		cm := hostileConfigMap(false)
		cm.Annotations = map[string]string{entity.IndexDataAnnotation: lookalike}
		ev := entity.ConfigMapToEvent(cm, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
		if ev.Metadata["indexed"] != "false" {
			t.Fatalf("annotation value %q should NOT enable indexing; indexed=%q", lookalike, ev.Metadata["indexed"])
		}
	}
}

func TestConfigMapToEvent_AllowListIsClosedAndStable(t *testing.T) {
	cm := hostileConfigMap(false)
	ev := entity.ConfigMapToEvent(cm, entity.ActionCreated, fixedWorkspace, fixedClusterID, "prod-eu", fixedNow)
	allowed := map[string]struct{}{
		"cluster": {}, "cluster_id": {}, "namespace": {}, "name": {}, "kind": {}, "emitter": {},
		"data_keys": {}, "data_count": {}, "indexed": {},
	}
	for k := range ev.Metadata {
		if _, ok := allowed[k]; !ok {
			t.Fatalf("ConfigMap metadata leaked unexpected key %q", k)
		}
	}
	if ev.Metadata["kind"] != "ConfigMap" || ev.Metadata["emitter"] != "k8s-operator" {
		t.Fatalf("base allow-list violation: %v", ev.Metadata)
	}
}
