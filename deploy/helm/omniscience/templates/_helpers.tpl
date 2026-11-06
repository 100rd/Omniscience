{{- define "omniscience.fullname" -}}
{{- default .Chart.Name .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "omniscience.labels" -}}
app.kubernetes.io/name: omniscience
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: management-local-observe-v1
{{- end -}}

{{- define "omniscience.secretName" -}}
{{- default (printf "%s-secret" (include "omniscience.fullname" .)) .Values.secrets.existingSecret -}}
{{- end -}}
