// cronjob_mapper_test.go: pure unit tests for CronJobToEvent (#210).
package entity_test

import (
	"testing"
	"time"

	"github.com/google/uuid"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/100rd/omniscience/operator/internal/entity"
)

var (
	cjTestWorkspace   = uuid.MustParse("11111111-2222-3333-4444-555555555555")
	cjTestClusterID   = uuid.MustParse("99999999-8888-7777-6666-555555555555")
	cjTestClusterName = "prod-eu"
	cjTestNow         = time.Date(2026, 5, 4, 12, 0, 0, 0, time.UTC)
)

func cjBoolPtr(b bool) *bool      { return &b }
func cjInt64Ptr(i int64) *int64   { return &i }
func cjInt32Ptr(i int32) *int32   { return &i }
func cjStrPtr(s string) *string   { return &s }

// ─── Envelope ─────────────────────────────────────────────────────────────

func TestCronJobToEvent_EnvelopeStampsWorkspaceFromConfig(t *testing.T) {
	cj := &batchv1.CronJob{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "team-a",
			Name:      "nightly-backup",
			Labels: map[string]string{
				"omniscience.io/workspace": "evil-ws",
				"team":                     "platform",
			},
			Annotations: map[string]string{
				"omniscience.io/index-data": "true",
			},
		},
		Spec: batchv1.CronJobSpec{Schedule: "0 2 * * *"},
	}
	ev := entity.CronJobToEvent(cj, entity.ActionUpdated, cjTestWorkspace, cjTestClusterID, cjTestClusterName, cjTestNow)

	if ev.WorkspaceID != cjTestWorkspace {
		t.Fatalf("workspace_id = %s, want %s — adversarial label honoured!", ev.WorkspaceID, cjTestWorkspace)
	}
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/CronJob/team-a/nightly-backup" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.URI != "kube://prod-eu/team-a/CronJob/nightly-backup" {
		t.Fatalf("uri = %q", ev.URI)
	}
	if ev.Metadata["schedule"] != "0 2 * * *" {
		t.Fatalf("schedule = %q", ev.Metadata["schedule"])
	}
}

// ACL invariant: only the closed allow-list of metadata keys is emitted.
func TestCronJobToEvent_ACL_NoUserLabelsOrAnnotationsLeak(t *testing.T) {
	cj := &batchv1.CronJob{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "team-a",
			Name:      "nightly-backup",
			Labels: map[string]string{
				"team":          "platform",
				"cost-center":   "eng-42",
				"app.k8s.io/of": "checkout",
			},
			Annotations: map[string]string{
				"description": "team-a nightly backup",
				"owner":       "alice@example.com",
			},
		},
		Spec: batchv1.CronJobSpec{Schedule: "0 2 * * *"},
	}
	ev := entity.CronJobToEvent(cj, entity.ActionUpdated, cjTestWorkspace, cjTestClusterID, cjTestClusterName, cjTestNow)

	allowed := map[string]struct{}{
		"cluster": {}, "cluster_id": {}, "namespace": {}, "name": {}, "kind": {}, "emitter": {},
		"schedule": {}, "time_zone": {}, "suspend": {}, "concurrency_policy": {},
		"starting_deadline_seconds": {}, "successful_jobs_history_limit": {}, "failed_jobs_history_limit": {},
		"last_schedule_time": {}, "last_successful_time": {}, "active_count": {},
	}
	for k := range ev.Metadata {
		if _, ok := allowed[k]; !ok {
			t.Fatalf("metadata leaked key %q (value=%q) — ACL allow-list violated", k, ev.Metadata[k])
		}
	}
	for forbidden := range cj.Labels {
		if v, ok := ev.Metadata[forbidden]; ok {
			t.Fatalf("user label %q leaked into metadata as %q", forbidden, v)
		}
	}
	for forbidden := range cj.Annotations {
		if v, ok := ev.Metadata[forbidden]; ok {
			t.Fatalf("user annotation %q leaked into metadata as %q", forbidden, v)
		}
	}
}

// ─── Fixture 1: minimal — only required schedule ──────────────────────────

func TestCronJobToEvent_MinimalSchedule(t *testing.T) {
	cj := &batchv1.CronJob{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "nightly"},
		Spec:       batchv1.CronJobSpec{Schedule: "0 2 * * *"},
	}
	ev := entity.CronJobToEvent(cj, entity.ActionCreated, cjTestWorkspace, cjTestClusterID, cjTestClusterName, cjTestNow)

	if ev.Metadata["schedule"] != "0 2 * * *" {
		t.Fatalf("schedule = %q", ev.Metadata["schedule"])
	}
	// All optional fields render as empty when nil/unset.
	for _, k := range []string{
		"time_zone", "suspend", "starting_deadline_seconds",
		"successful_jobs_history_limit", "failed_jobs_history_limit",
		"last_schedule_time", "last_successful_time",
	} {
		if ev.Metadata[k] != "" {
			t.Fatalf("optional field %q should be empty when unset; got %q", k, ev.Metadata[k])
		}
	}
	if ev.Metadata["active_count"] != "0" {
		t.Fatalf("active_count = %q, want 0", ev.Metadata["active_count"])
	}
}

// ─── Fixture 2: with timezone, suspend=true, concurrency Forbid ───────────

func TestCronJobToEvent_WithTimezoneSuspendConcurrency(t *testing.T) {
	cj := &batchv1.CronJob{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "weekly"},
		Spec: batchv1.CronJobSpec{
			Schedule:                   "0 9 * * 1",
			TimeZone:                   cjStrPtr("Europe/Berlin"),
			Suspend:                    cjBoolPtr(true),
			ConcurrencyPolicy:          batchv1.ForbidConcurrent,
			StartingDeadlineSeconds:    cjInt64Ptr(60),
			SuccessfulJobsHistoryLimit: cjInt32Ptr(3),
			FailedJobsHistoryLimit:     cjInt32Ptr(1),
		},
	}
	ev := entity.CronJobToEvent(cj, entity.ActionUpdated, cjTestWorkspace, cjTestClusterID, cjTestClusterName, cjTestNow)

	if ev.Metadata["time_zone"] != "Europe/Berlin" {
		t.Fatalf("time_zone = %q", ev.Metadata["time_zone"])
	}
	if ev.Metadata["suspend"] != "true" {
		t.Fatalf("suspend = %q", ev.Metadata["suspend"])
	}
	if ev.Metadata["concurrency_policy"] != "Forbid" {
		t.Fatalf("concurrency_policy = %q", ev.Metadata["concurrency_policy"])
	}
	if ev.Metadata["starting_deadline_seconds"] != "60" {
		t.Fatalf("starting_deadline_seconds = %q", ev.Metadata["starting_deadline_seconds"])
	}
	if ev.Metadata["successful_jobs_history_limit"] != "3" {
		t.Fatalf("successful_jobs_history_limit = %q", ev.Metadata["successful_jobs_history_limit"])
	}
	if ev.Metadata["failed_jobs_history_limit"] != "1" {
		t.Fatalf("failed_jobs_history_limit = %q", ev.Metadata["failed_jobs_history_limit"])
	}
}

// ─── Fixture 3: with status.lastScheduleTime / lastSuccessfulTime / active ──

func TestCronJobToEvent_WithStatusFields(t *testing.T) {
	scheduledAt := time.Date(2026, 5, 4, 2, 0, 0, 0, time.UTC)
	successAt := time.Date(2026, 5, 4, 2, 1, 23, 0, time.UTC)
	cj := &batchv1.CronJob{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "nightly"},
		Spec:       batchv1.CronJobSpec{Schedule: "0 2 * * *"},
		Status: batchv1.CronJobStatus{
			LastScheduleTime:   &metav1.Time{Time: scheduledAt},
			LastSuccessfulTime: &metav1.Time{Time: successAt},
			Active: []corev1.ObjectReference{
				{Name: "nightly-1234567890"},
				{Name: "nightly-1234567891"},
			},
		},
	}
	ev := entity.CronJobToEvent(cj, entity.ActionUpdated, cjTestWorkspace, cjTestClusterID, cjTestClusterName, cjTestNow)

	if ev.Metadata["last_schedule_time"] != "2026-05-04T02:00:00Z" {
		t.Fatalf("last_schedule_time = %q", ev.Metadata["last_schedule_time"])
	}
	if ev.Metadata["last_successful_time"] != "2026-05-04T02:01:23Z" {
		t.Fatalf("last_successful_time = %q", ev.Metadata["last_successful_time"])
	}
	if ev.Metadata["active_count"] != "2" {
		t.Fatalf("active_count = %q", ev.Metadata["active_count"])
	}
}

// ─── Fixture 4: suspend=false (distinct from nil) ─────────────────────────

func TestCronJobToEvent_SuspendFalseDistinctFromNil(t *testing.T) {
	cj := &batchv1.CronJob{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "x"},
		Spec: batchv1.CronJobSpec{
			Schedule: "0 2 * * *",
			Suspend:  cjBoolPtr(false),
		},
	}
	ev := entity.CronJobToEvent(cj, entity.ActionUpdated, cjTestWorkspace, cjTestClusterID, cjTestClusterName, cjTestNow)
	if ev.Metadata["suspend"] != "false" {
		t.Fatalf("suspend = %q, want explicit false (distinct from nil)", ev.Metadata["suspend"])
	}
}

// ─── Default namespace fallback ───────────────────────────────────────────

func TestCronJobToEvent_EmptyNamespaceFallsBackToDefault(t *testing.T) {
	cj := &batchv1.CronJob{Spec: batchv1.CronJobSpec{Schedule: "@daily"}}
	cj.Name = "x"
	ev := entity.CronJobToEvent(cj, entity.ActionUpdated, cjTestWorkspace, cjTestClusterID, cjTestClusterName, cjTestNow)
	if ev.ExternalID != "k8s_resource/99999999-8888-7777-6666-555555555555/CronJob/default/x" {
		t.Fatalf("external_id = %q", ev.ExternalID)
	}
	if ev.Metadata["namespace"] != "default" {
		t.Fatalf("namespace = %q", ev.Metadata["namespace"])
	}
}

// ─── JobTemplate is intentionally NOT emitted ─────────────────────────────

func TestCronJobToEvent_JobTemplateNotEmitted(t *testing.T) {
	// Even with a complex JobTemplate, we should not emit anything from it.
	cj := &batchv1.CronJob{
		ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "x"},
		Spec: batchv1.CronJobSpec{
			Schedule: "0 2 * * *",
			JobTemplate: batchv1.JobTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels: map[string]string{"would-leak": "yes"},
				},
			},
		},
	}
	ev := entity.CronJobToEvent(cj, entity.ActionUpdated, cjTestWorkspace, cjTestClusterID, cjTestClusterName, cjTestNow)
	for k, v := range ev.Metadata {
		if v == "yes" {
			t.Fatalf("JobTemplate label leaked into metadata under key %q", k)
		}
		if k == "job_template" || k == "job_template_json" {
			t.Fatalf("JobTemplate emitted as %q — should be left to Job watcher (#157)", k)
		}
	}
}
