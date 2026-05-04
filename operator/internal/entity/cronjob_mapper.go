// cronjob_mapper.go: batchv1.CronJob -> Event mapper (issue #210).
//
// CronJob is a namespaced scheduled batch workload. The mapper emits ONLY
// the closed allow-list of fields from baseMetadata plus the schedule,
// suspend / concurrency / history settings, and current status.
//
// SECURITY POSTURE — same as every other operator mapper:
//
//   - User-supplied labels and annotations are NEVER copied through.
//   - `namespace` appears in baseMetadata + external_id ONLY.
//   - The embedded `spec.jobTemplate` is NOT emitted — the actual Jobs that
//     get created are already covered by the Job watcher (#157). Emitting
//     the template here would duplicate without graph value.
package entity

import (
	"strconv"
	"time"

	"github.com/google/uuid"
	batchv1 "k8s.io/api/batch/v1"
)

// EntityKindCronJob is the K8s kind segment in CronJob external_ids.
const EntityKindCronJob = "CronJob"

// CronJobToEvent maps a batchv1.CronJob plus an action into an Event.
// Pure function — no I/O, no clients, no clock beyond `now`.
//
// Emitted metadata:
//
//   - Base allow-list: cluster, cluster_id, namespace, name, kind, emitter
//   - "schedule":                       cron expression string
//   - "time_zone":                      string; empty when nil
//   - "suspend":                        "true" / "false" / "" (nil)
//   - "concurrency_policy":             Allow / Forbid / Replace; empty when nil
//   - "starting_deadline_seconds":      decimal string; empty when nil
//   - "successful_jobs_history_limit":  decimal string; empty when nil
//   - "failed_jobs_history_limit":      decimal string; empty when nil
//   - "last_schedule_time":             RFC3339 string; empty when nil
//   - "last_successful_time":           RFC3339 string; empty when nil
//   - "active_count":                   decimal string of len(.Active)
func CronJobToEvent(cj *batchv1.CronJob, action Action, workspaceID uuid.UUID, clusterID uuid.UUID, clusterName string, now time.Time) *Event {
	namespace := resolveNamespace(cj.Namespace)
	meta := baseMetadata(clusterID, clusterName, namespace, EntityKindCronJob, cj.Name)

	meta["schedule"] = cj.Spec.Schedule
	if cj.Spec.TimeZone != nil {
		meta["time_zone"] = *cj.Spec.TimeZone
	} else {
		meta["time_zone"] = ""
	}

	meta["suspend"] = boolPtrToTriState(cj.Spec.Suspend)
	meta["concurrency_policy"] = string(cj.Spec.ConcurrencyPolicy)

	meta["starting_deadline_seconds"] = int64PtrToString(cj.Spec.StartingDeadlineSeconds)
	meta["successful_jobs_history_limit"] = int32PtrToString(cj.Spec.SuccessfulJobsHistoryLimit)
	meta["failed_jobs_history_limit"] = int32PtrToString(cj.Spec.FailedJobsHistoryLimit)

	if cj.Status.LastScheduleTime != nil {
		meta["last_schedule_time"] = cj.Status.LastScheduleTime.UTC().Format(time.RFC3339)
	} else {
		meta["last_schedule_time"] = ""
	}
	if cj.Status.LastSuccessfulTime != nil {
		meta["last_successful_time"] = cj.Status.LastSuccessfulTime.UTC().Format(time.RFC3339)
	} else {
		meta["last_successful_time"] = ""
	}
	meta["active_count"] = strconv.Itoa(len(cj.Status.Active))

	return &Event{
		SourceID:    DeriveSourceID(workspaceID, clusterName),
		SourceType:  SourceType,
		ExternalID:  externalIDFor(clusterID, EntityKindCronJob, namespace, cj.Name),
		URI:         uriFor(clusterName, namespace, EntityKindCronJob, cj.Name),
		Action:      action,
		WorkspaceID: workspaceID,
		EmittedAt:   now.UTC(),
		Metadata:    meta,
	}
}

// boolPtrToTriState renders *bool as "true" / "false" / "" (nil). Distinct
// from a literal false so consumers can tell "explicitly nil" (defer to K8s
// default which is `false`) from "explicitly false". Same shape used by the
// ServiceAccount mapper (#206) for AutomountServiceAccountToken.
func boolPtrToTriState(b *bool) string {
	if b == nil {
		return ""
	}
	if *b {
		return "true"
	}
	return "false"
}

// int64PtrToString renders *int64 as decimal string; empty when nil.
func int64PtrToString(p *int64) string {
	if p == nil {
		return ""
	}
	return strconv.FormatInt(*p, 10)
}

// int32PtrToString renders *int32 as decimal string; empty when nil.
func int32PtrToString(p *int32) string {
	if p == nil {
		return ""
	}
	return strconv.FormatInt(int64(*p), 10)
}
