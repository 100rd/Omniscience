{{/*
Production-HA fail-closed evidence + policy guard (issue #350).

  AC-HA-1    dedicated-account EKS target + external HA stateful evidence
  AC-HA-2    deterministic HA scheduling policy (replicas / autoscaling floor)
  AC-HA-3    per-stateful-dependency topology + entitlement evidence
  AC-HA-5    bundled stateful charts / single-replica installs cannot claim
             production-ha posture
  REQ-OPS-6  disabling reconciliation/retention cannot be presented as
             production healthy

No-op when production.enabled is false. When true, each check below either
passes silently or calls `fail` naming the exact missing/invalid field —
Helm aborts the render rather than emitting a partially-qualified profile.
Included from deployment.yaml so it always evaluates during template/install.
*/}}
{{- define "omniscience.productionGuards" -}}
{{- if .Values.production.enabled }}
{{- $ev := .Values.production.evidence }}
{{- $prod := .Values.production }}

{{- /* AC-HA-1: dedicated-account / cluster / region / zone-placement evidence */}}
{{- if not $ev.account }}
{{- fail "production.evidence.account is required when production.enabled=true (dedicated-account EKS target evidence)" }}
{{- end }}
{{- if not $ev.cluster }}
{{- fail "production.evidence.cluster is required when production.enabled=true (dedicated-account EKS target evidence)" }}
{{- end }}
{{- if not $ev.region }}
{{- fail "production.evidence.region is required when production.enabled=true (dedicated-account EKS target evidence)" }}
{{- end }}
{{- if ne (len $ev.zones) 3 }}
{{- fail (printf "production.evidence.zones must list exactly 3 availability zones when production.enabled=true (got %d)" (len $ev.zones)) }}
{{- end }}
{{- range $zone := $ev.zones }}
{{- if not $zone }}
{{- fail "production.evidence.zones must not contain an empty zone name when production.enabled=true" }}
{{- end }}
{{- end }}

{{- /* AC-HA-2: deterministic HA scheduling policy floor */}}
{{- if lt (int .Values.replicaCount) 3 }}
{{- fail (printf "replicaCount must be >= 3 when production.enabled=true (got %d)" (int .Values.replicaCount)) }}
{{- end }}
{{- if lt (int $prod.autoscaling.minReplicas) 3 }}
{{- fail (printf "production.autoscaling.minReplicas must be >= 3 when production.enabled=true (got %d)" (int $prod.autoscaling.minReplicas)) }}
{{- end }}

{{- /* AC-HA-5: bundled stateful subcharts are evaluation-only, never production */}}
{{- if .Values.postgresql.enabled }}
{{- fail "postgresql.enabled must be false when production.enabled=true (bundled stateful charts are evaluation-only — supply production.evidence.rds instead)" }}
{{- end }}
{{- if .Values.neo4j.enabled }}
{{- fail "neo4j.enabled must be false when production.enabled=true (bundled stateful charts are evaluation-only — supply production.evidence.neo4j instead)" }}
{{- end }}
{{- if .Values.qdrant.enabled }}
{{- fail "qdrant.enabled must be false when production.enabled=true (bundled stateful charts are evaluation-only — supply production.evidence.qdrant instead)" }}
{{- end }}
{{- if .Values.nats.enabled }}
{{- fail "nats.enabled must be false when production.enabled=true (bundled stateful charts are evaluation-only — supply production.evidence.jetstream instead)" }}
{{- end }}
{{- if eq (toString .Values.postgres.enabled) "true" }}
{{- fail "postgres.enabled must be false when production.enabled=true (bundled in-cluster Postgres (postgres.enabled) is evaluation-only; production requires external RDS Multi-AZ (production.evidence.rds) (AC-HA-1/5))" }}
{{- end }}

{{- /* AC-HA-3: RDS is the Multi-AZ authority */}}
{{- if not $ev.rds.endpoint }}
{{- fail "production.evidence.rds.endpoint is required when production.enabled=true (external RDS Multi-AZ authority evidence)" }}
{{- end }}
{{- if ne (toString $ev.rds.multiAz) "true" }}
{{- fail "production.evidence.rds.multiAz must be true when production.enabled=true (RDS must be the Multi-AZ authority)" }}
{{- end }}

{{- /* AC-HA-3: JetStream — 3 replicas, external cluster */}}
{{- if not $ev.jetstream.externalUrl }}
{{- fail "production.evidence.jetstream.externalUrl is required when production.enabled=true (external JetStream cluster evidence)" }}
{{- end }}
{{- if lt (int $ev.jetstream.replicas) 3 }}
{{- fail (printf "production.evidence.jetstream.replicas must be >= 3 when production.enabled=true (got %d)" (int $ev.jetstream.replicas)) }}
{{- end }}

{{- /* AC-HA-3: Neo4j — approved HA topology + entitlement reference (never a literal secret) */}}
{{- if not $ev.neo4j.topology }}
{{- fail "production.evidence.neo4j.topology is required when production.enabled=true (approved Neo4j HA topology evidence)" }}
{{- end }}
{{- if not $ev.neo4j.entitlementRef }}
{{- fail "production.evidence.neo4j.entitlementRef is required when production.enabled=true (license/managed-service entitlement reference — never a literal secret value)" }}
{{- end }}

{{- /* AC-HA-3: Qdrant — distributed, >= 3 nodes */}}
{{- if not $ev.qdrant.topology }}
{{- fail "production.evidence.qdrant.topology is required when production.enabled=true (distributed Qdrant topology evidence)" }}
{{- end }}
{{- if lt (int $ev.qdrant.nodes) 3 }}
{{- fail (printf "production.evidence.qdrant.nodes must be >= 3 when production.enabled=true (got %d)" (int $ev.qdrant.nodes)) }}
{{- end }}

{{- /* REQ-OPS-6: disabling reconciliation/retention cannot be presented as production healthy */}}
{{- if ne (toString .Values.retention.enabled) "true" }}
{{- fail "cannot claim production-ha with retention/reconciliation disabled (REQ-OPS-6): retention.enabled must be true when production.enabled=true" }}
{{- end }}
{{- if ne (toString .Values.config.reconcileEnabled) "true" }}
{{- fail "cannot claim production-ha with retention/reconciliation disabled (REQ-OPS-6): config.reconcileEnabled must be true when production.enabled=true" }}
{{- end }}

{{- end }}
{{- end }}
