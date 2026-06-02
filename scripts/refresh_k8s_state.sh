#!/bin/bash
# Live re-sync of qbiq-shared K8s/ArgoCD state into Omniscience.
# Re-extracts read-only creds via kubectl each run (no token persisted on disk),
# fetches current cluster state, and re-seeds documents + graph (idempotent).
# Scheduled via launchd (com.omniscience.k8s-refresh) or run manually.
set -euo pipefail

OMNI=/Users/igor_gerasimov/Develop/multi-team-agentic/project/Omniscience
NS=omniscience-reader
SECRET=omniscience-reader-token
cd "$OMNI"

# 1. Fresh read-only creds from the cluster (uses the operator's own kubeconfig).
kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' > /tmp/k8s_api.txt
kubectl get secret "$SECRET" -n "$NS" -o jsonpath='{.data.token}'  | base64 -d > /tmp/k8s_sa_token.txt
kubectl get secret "$SECRET" -n "$NS" -o jsonpath='{.data.ca\.crt}' | base64 -d > /tmp/k8s_ca.pem

# 2. Fetch current state -> /tmp/k8s_docs.json
python3 "$OMNI/scripts/fetch_k8s_state.py"

# 3. Re-seed documents (vectors) + graph (entities/edges) into the running stack.
docker compose cp /tmp/k8s_docs.json app:/tmp/docs.json
docker compose cp /tmp/k8s_docs.json app:/tmp/k8s_docs.json
docker compose cp "$OMNI/scripts/seed_from_docs.py" app:/tmp/seed_from_docs.py
docker compose cp "$OMNI/scripts/seed_k8s_graph.py"  app:/tmp/seed_k8s_graph.py
docker compose exec -T app /app/.venv/bin/python /tmp/seed_from_docs.py k8s-qbiq-shared
docker compose exec -T app /app/.venv/bin/python /tmp/seed_k8s_graph.py

echo "k8s-qbiq-shared re-synced at $(date '+%Y-%m-%d %H:%M:%S')"
