{{/*
Shared read-admission fail-closed evidence guard (issue #350, AC-SCALE-5).

  AC-SCALE-1  backend identity required before a deployment may claim
              shared admission (never a silent process-local bypass)
  AC-SCALE-2  SRE incident vs background lane budgets are explicit inputs
  AC-SCALE-4  failure-reserve fraction is an explicit, bounded input

Gated on its OWN `production.admission.enabled` flag — deliberately
INDEPENDENT of `production.enabled`. The production-HA profile (issue #350
slice "production-HA", already merged — see omniscience.productionGuards /
_production_guards.tpl) must keep rendering and qualifying without ever
being forced to supply admission evidence; shared admission is an
additive, separately-activated posture layered on top of it, not a new
requirement retrofitted onto the existing one.

No-op when production.admission.enabled is false. When true, each check
below either passes silently or calls `fail` naming the exact missing/
invalid field — same fail-closed pattern as omniscience.productionGuards:
Helm aborts the render rather than emitting a partially-qualified profile.
Included from deployment.yaml so it always evaluates during template/install.
*/}}
{{- define "omniscience.admissionGuards" -}}
{{- if .Values.production.admission.enabled }}
{{- $adm := .Values.production.admission }}

{{- /* AC-SCALE-1: backend identity */}}
{{- if not $adm.backend }}
{{- fail "production.admission.backend is required when production.admission.enabled=true (shared read-admission backend identity, AC-SCALE-1)" }}
{{- end }}
{{- if eq $adm.backend "disabled" }}
{{- fail "production.admission.backend must not be 'disabled' when production.admission.enabled=true (AC-SCALE-1: a named shared-admission backend is required, never the process-local default)" }}
{{- end }}
{{- if not $adm.backendIdentity }}
{{- fail "production.admission.backendIdentity is required when production.admission.enabled=true (human-approved concrete backend descriptor, AC-SCALE-1)" }}
{{- end }}

{{- /* AC-SCALE-2: lane budgets */}}
{{- if lt (int $adm.laneIncidentBudget) 1 }}
{{- fail "production.admission.laneIncidentBudget must be >= 1 when production.admission.enabled=true (AC-SCALE-2)" }}
{{- end }}
{{- if lt (int $adm.laneBackgroundBudget) 1 }}
{{- fail "production.admission.laneBackgroundBudget must be >= 1 when production.admission.enabled=true (AC-SCALE-2)" }}
{{- end }}

{{- /* Bounded admission queue */}}
{{- if lt (int $adm.queueDepthMax) 1 }}
{{- fail "production.admission.queueDepthMax must be >= 1 when production.admission.enabled=true (bounded admission queue, AC-SCALE-2)" }}
{{- end }}

{{- /* Latency/error thresholds the incident lane must stay within */}}
{{- if not (gt (float64 $adm.latencyThresholdMs) 0.0) }}
{{- fail "production.admission.latencyThresholdMs must be > 0 when production.admission.enabled=true (AC-SCALE-2/5 fairness threshold)" }}
{{- end }}
{{- if not (gt (float64 $adm.errorRateThreshold) 0.0) }}
{{- fail "production.admission.errorRateThreshold must be > 0 when production.admission.enabled=true (AC-SCALE-2/5 fairness threshold)" }}
{{- end }}

{{- /* AC-SCALE-4: bounded failure reserve, never unbounded */}}
{{- if not (gt (float64 $adm.failureReserveFraction) 0.0) }}
{{- fail "production.admission.failureReserveFraction must be > 0 when production.admission.enabled=true (bounded incident reserve on backend loss, AC-SCALE-4)" }}
{{- end }}
{{- if gt (float64 $adm.failureReserveFraction) 1.0 }}
{{- fail "production.admission.failureReserveFraction must be <= 1.0 when production.admission.enabled=true (AC-SCALE-4: never an unbounded reserve)" }}
{{- end }}

{{- end }}
{{- end }}
