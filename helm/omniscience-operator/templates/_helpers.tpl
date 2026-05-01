{{/*
Expand the name of the chart.
*/}}
{{- define "omniscience-operator.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully qualified app name. Truncated at 63 chars (DNS-1123 label).
*/}}
{{- define "omniscience-operator.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Chart label.
*/}}
{{- define "omniscience-operator.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels applied to every resource.
*/}}
{{- define "omniscience-operator.labels" -}}
helm.sh/chart: {{ include "omniscience-operator.chart" . }}
{{ include "omniscience-operator.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: omniscience
{{- end -}}

{{/*
Selector labels — used by Deployment selector and Service selector.
*/}}
{{- define "omniscience-operator.selectorLabels" -}}
app.kubernetes.io/name: {{ include "omniscience-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
ServiceAccount name.
*/}}
{{- define "omniscience-operator.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "omniscience-operator.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Parse a URL into host and port at template-render time. Returns a dict with
keys `host`, `port`, `scheme`. Used by the NetworkPolicy to emit explicit
egress rules to nats.url and omniscience.serverUrl.

Inputs accepted:
  nats://host:4222
  nats://host:4222/path
  https://host:443
  http://host:8000
  host:port           (no scheme — assumed nats)

Defaults:
  nats   -> 4222
  http   -> 80
  https  -> 443

If the URL is empty or unparseable the helper returns an empty dict so
callers can guard with `if`.
*/}}
{{- define "omniscience-operator.parseURL" -}}
{{- $url := . -}}
{{- if not $url -}}
{{- dict | toJson -}}
{{- else -}}
{{- $scheme := "" -}}
{{- $rest := $url -}}
{{- if contains "://" $url -}}
{{- $parts := splitList "://" $url -}}
{{- $scheme = index $parts 0 -}}
{{- $rest = index $parts 1 -}}
{{- else -}}
{{- $scheme = "nats" -}}
{{- end -}}
{{- /* Drop any trailing path/query: take everything before the first '/' or '?' */ -}}
{{- $hostport := $rest -}}
{{- if contains "/" $hostport -}}
{{- $hostport = (splitList "/" $hostport | first) -}}
{{- end -}}
{{- if contains "?" $hostport -}}
{{- $hostport = (splitList "?" $hostport | first) -}}
{{- end -}}
{{- $host := $hostport -}}
{{- $port := "" -}}
{{- if contains ":" $hostport -}}
{{- $hp := splitList ":" $hostport -}}
{{- $host = index $hp 0 -}}
{{- $port = index $hp 1 -}}
{{- else -}}
{{- if eq $scheme "https" -}}{{- $port = "443" -}}
{{- else if eq $scheme "http" -}}{{- $port = "80" -}}
{{- else -}}{{- $port = "4222" -}}
{{- end -}}
{{- end -}}
{{- (dict "host" $host "port" $port "scheme" $scheme) | toJson -}}
{{- end -}}
{{- end -}}

{{/*
Resolve the operator's own image reference. When `image.digest` is set
(`sha256:...`), the manifest renders `repository@digest` (production-recommended,
immutable). Otherwise renders `repository:tag` (default).
*/}}
{{- define "omniscience-operator.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}
{{- end -}}
