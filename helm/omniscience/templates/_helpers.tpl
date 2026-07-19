{{/*
Expand the name of the chart.
*/}}
{{- define "omniscience.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "omniscience.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart label.
*/}}
{{- define "omniscience.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "omniscience.labels" -}}
helm.sh/chart: {{ include "omniscience.chart" . }}
{{ include "omniscience.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
omniscience.io/posture: {{ include "omniscience.posture" . }}
{{- end }}

{{/*
Posture — evaluation | governed | production-ha (AC-HA-5, REQ-OPS-6).

Derived, not user-settable. REQ-OPS-6 mapping:

  - "production-ha" requires production.enabled=true, which in turn only
    renders (see _production_guards.tpl, included from deployment.yaml) once
    every AC-HA-1/2/3 evidence/policy guard passes AND retention +
    reconciliation stay enabled — REQ-OPS-6 forbids presenting a build with
    reconciliation/retention disabled as production healthy. A caller cannot
    `--set` this label directly to claim production-ha without satisfying
    those guards first — the guard `fail`s the render before this helper
    would ever compute "production-ha" for an unqualified install.
  - "governed" is the explicit intermediate signal: production.enabled is
    still false, but at least one production.evidence.* field has been
    populated by hand (an external stateful dependency wired in ahead of a
    full production cutover) — hardened beyond the bare default, but not a
    qualified production claim.
  - "evaluation" is the REQ-OPS-6 fallback: no production.enabled and no
    evidence signal at all resolves to evaluation-only. An unrecognized/
    unpopulated configuration never silently resolves to something stronger.
*/}}
{{- define "omniscience.posture" -}}
{{- if .Values.production.enabled -}}
production-ha
{{- else -}}
{{- $ev := .Values.production.evidence -}}
{{- $hasEvidenceSignal := or $ev.account $ev.cluster $ev.region (gt (len $ev.zones) 0) $ev.rds.endpoint $ev.rds.multiAz $ev.jetstream.externalUrl (gt (int $ev.jetstream.replicas) 0) $ev.neo4j.topology $ev.neo4j.entitlementRef $ev.qdrant.topology (gt (int $ev.qdrant.nodes) 0) -}}
{{- if $hasEvidenceSignal -}}
governed
{{- else -}}
evaluation
{{- end -}}
{{- end -}}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "omniscience.selectorLabels" -}}
app.kubernetes.io/name: {{ include "omniscience.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
ServiceAccount name.
*/}}
{{- define "omniscience.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "omniscience.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Database URL — internal or external.
*/}}
{{- define "omniscience.databaseUrl" -}}
{{- if .Values.postgres.externalUrl }}
{{- .Values.postgres.externalUrl }}
{{- else }}
{{- printf "postgresql://%s@%s-postgres:5432/%s" .Values.postgres.user (include "omniscience.fullname" .) .Values.postgres.database }}
{{- end }}
{{- end }}

{{/*
NATS URL — internal or external.
*/}}
{{- define "omniscience.natsUrl" -}}
{{- if .Values.nats.externalUrl }}
{{- .Values.nats.externalUrl }}
{{- else }}
{{- printf "nats://%s-nats:4222" (include "omniscience.fullname" .) }}
{{- end }}
{{- end }}
