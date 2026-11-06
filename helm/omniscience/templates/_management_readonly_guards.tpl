{{/*
management-readonly-v1 fail-closed configuration guard
(task-sp-86-management-readonly-release, ADR-0022).

  invariant 1   select management-readonly-v1 by immutable id and revision
  invariant 2   network traffic restricted to selected runtime owners
  invariant 3   no Omnius endpoint/credential/service-discovery entry
  invariant 4   select only the Reliability domain pack
  invariant 5   pin exact SP-60 policy and SP-10/SP-81 producer digests
  invariant 7   every owner/component/management/privacy mutation route disabled
  invariant 9   Omnius status is exactly "not_deployed"

No-op when managementReadonly.enabled is false. When true, each check below
either passes silently or calls `fail` naming the exact missing/invalid
field -- same fail-closed pattern as omniscience.productionGuards /
omniscience.admissionGuards: Helm aborts the render rather than emitting a
partially-qualified profile. Included from deployment.yaml so it always
evaluates during template/install.
*/}}
{{- define "omniscience.managementReadonlyGuards" -}}
{{- if .Values.managementReadonly.enabled }}
{{- $mr := .Values.managementReadonly }}

{{- /* invariant 1: immutable id and revision */}}
{{- if ne $mr.profileId "management-readonly-v1" }}
{{- fail "managementReadonly.profileId must be exactly 'management-readonly-v1' when managementReadonly.enabled=true (ADR-0022 invariant 1)" }}
{{- end }}
{{- if not $mr.profileRevision }}
{{- fail "managementReadonly.profileRevision is required when managementReadonly.enabled=true (immutable release revision, ADR-0022 invariant 1)" }}
{{- end }}

{{- /* invariant 4: exactly the Reliability domain pack, nothing wider */}}
{{- if ne (len $mr.domainPacks) 1 }}
{{- fail (printf "managementReadonly.domainPacks must list exactly one domain pack when managementReadonly.enabled=true (got %d, ADR-0022 invariant 4)" (len $mr.domainPacks)) }}
{{- end }}
{{- if ne (index $mr.domainPacks 0) "reliability" }}
{{- fail "managementReadonly.domainPacks must be ['reliability'] when managementReadonly.enabled=true (ADR-0022 invariant 4: no other domain pack is selected in this profile)" }}
{{- end }}

{{- /* invariant 7: mutation routes disabled */}}
{{- if not $mr.mutationRoutesDisabled }}
{{- fail "managementReadonly.mutationRoutesDisabled must be true when managementReadonly.enabled=true (ADR-0022 invariant 7: no management verdict, owner mutation, or generic query surface)" }}
{{- end }}

{{- /* invariant 2: network policy restricted to selected owners */}}
{{- if not $mr.networkPolicy.restrictToSelectedOwners }}
{{- fail "managementReadonly.networkPolicy.restrictToSelectedOwners must be true when managementReadonly.enabled=true (ADR-0022 invariant 2)" }}
{{- end }}

{{- /* invariant 3/9: Omnius absent and explicitly not_deployed */}}
{{- if $mr.omnius.endpoint }}
{{- fail "managementReadonly.omnius.endpoint must be empty when managementReadonly.enabled=true (ADR-0022 invariant 3: no Omnius endpoint/credential/service-discovery entry)" }}
{{- end }}
{{- if ne $mr.omnius.status "not_deployed" }}
{{- fail "managementReadonly.omnius.status must be exactly 'not_deployed' when managementReadonly.enabled=true (ADR-0022 invariant 9)" }}
{{- end }}

{{- /* invariant 5: pinned dependency digests */}}
{{- if not $mr.dependencyDigests.sp10McpDigest }}
{{- fail "managementReadonly.dependencyDigests.sp10McpDigest is required when managementReadonly.enabled=true (ADR-0022 invariant 5)" }}
{{- end }}
{{- if not $mr.dependencyDigests.sp60PolicyDigest }}
{{- fail "managementReadonly.dependencyDigests.sp60PolicyDigest is required when managementReadonly.enabled=true (ADR-0022 invariant 5)" }}
{{- end }}
{{- if not $mr.dependencyDigests.sp81ManifestDigest }}
{{- fail "managementReadonly.dependencyDigests.sp81ManifestDigest is required when managementReadonly.enabled=true (ADR-0022 invariant 5)" }}
{{- end }}

{{- end }}
{{- end }}
