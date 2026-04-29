package entity_test

import (
	"testing"

	batchv1 "k8s.io/api/batch/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/omniscience/operator/internal/entity"
)

func newJob(namespace, name string, parallelism *int32, active int32, owners []metav1.OwnerReference) *batchv1.Job {
	return &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{
			Namespace:       namespace,
			Name:            name,
			Labels:          adversarialLabels,
			Annotations:     adversarialAnnotations,
			OwnerReferences: owners,
		},
		Spec: batchv1.JobSpec{
			Parallelism: parallelism,
		},
		Status: batchv1.JobStatus{
			Active: active,
		},
	}
}

func TestJobToEvent_FieldsAreCorrect(t *testing.T) {
	parallelism := int32(2)
	j := newJob("team-a", "nightly-export", &parallelism, 1, nil)
	ev := entity.JobToEvent(j, entity.ActionCreated, fixedWorkspace, "prod-eu", fixedNow)

	if want := "k8s_resource/Job/team-a/nightly-export"; ev.ExternalID != want {
		t.Fatalf("external_id = %q, want %q", ev.ExternalID, want)
	}
	if got := ev.Metadata["kind"]; got != "Job" {
		t.Fatalf("metadata[kind] = %q, want %q", got, "Job")
	}
	if got := ev.Metadata["replicas"]; got != "2" {
		t.Fatalf("metadata[replicas] = %q (parallelism)", got)
	}
	if got := ev.Metadata["available_replicas"]; got != "1" {
		t.Fatalf("metadata[available_replicas] = %q (status.active)", got)
	}
}

func TestJobToEvent_OwnerEdgeFromCronJob(t *testing.T) {
	// Forward-compat: a Job created by a CronJob carries an
	// ownerReference to that CronJob; even though the CronJob watcher is
	// deferred to a follow-up sub-issue, the edge information already
	// flows through this mapper.
	owners := []metav1.OwnerReference{
		{
			APIVersion: "batch/v1",
			Kind:       "CronJob",
			Name:       "nightly-export",
			UID:        "cj-uid",
		},
	}
	j := newJob("team-a", "nightly-export-1700000000", nil, 1, owners)
	ev := entity.JobToEvent(j, entity.ActionCreated, fixedWorkspace, "prod-eu", fixedNow)

	if len(ev.Edges) != 1 {
		t.Fatalf("edges count = %d, want 1; %#v", len(ev.Edges), ev.Edges)
	}
	if got := ev.Edges[0].FromKind; got != "CronJob" {
		t.Fatalf("edge from_kind = %q, want %q", got, "CronJob")
	}
	if got := ev.Edges[0].FromExternalID; got != "k8s_resource/CronJob/team-a/nightly-export" {
		t.Fatalf("edge from_external_id = %q", got)
	}
}

func TestJobToEvent_NilParallelismOmitsReplicas(t *testing.T) {
	// spec.parallelism is *int32 — nil means K8s default (1) but the
	// operator surfaces only what the resource itself says.
	j := newJob("team-a", "ad-hoc", nil, 0, nil)
	ev := entity.JobToEvent(j, entity.ActionCreated, fixedWorkspace, "prod-eu", fixedNow)
	if _, ok := ev.Metadata["replicas"]; ok {
		t.Fatalf("metadata[replicas] should be absent when parallelism is nil; got %q", ev.Metadata["replicas"])
	}
}

func TestJobToEvent_NoLabelLeakage(t *testing.T) {
	parallelism := int32(1)
	j := newJob("team-a", "ad-hoc", &parallelism, 0, nil)
	ev := entity.JobToEvent(j, entity.ActionUpdated, fixedWorkspace, "prod-eu", fixedNow)
	for k := range ev.Metadata {
		switch k {
		case "cluster", "namespace", "name", "kind", "emitter", "replicas", "available_replicas":
		default:
			t.Fatalf("leaked metadata key %q", k)
		}
	}
}
