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
