"""Fetch real cluster state (read-only) from the EKS API via the omniscience-reader
ServiceAccount token and emit documents for Omniscience to index.

Runs on the HOST (which has cluster access). Output: /tmp/k8s_docs.json — a list of
{external_id, uri, title, text, metadata} that the in-container seeder embeds + upserts.
"""

import json
import ssl
import urllib.request

API = open("/tmp/k8s_api.txt").read().strip()
TOKEN = open("/tmp/k8s_sa_token.txt").read().strip()
CA = "/tmp/k8s_ca.pem"
CLUSTER = "qbiq-shared"

_ctx = ssl.create_default_context(cafile=CA)


def _get(path: str) -> dict:
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, context=_ctx, timeout=20) as r:
        return json.loads(r.read())


docs: list[dict] = []


def add(external_id: str, uri: str, title: str, text: str, metadata: dict) -> None:
    docs.append(
        {
            "external_id": external_id,
            "uri": uri,
            "title": title,
            "text": text,
            "metadata": metadata,
        }
    )


# --- ArgoCD Applications (the headline: sync/health/repo/destination) ---
apps = _get("/apis/argoproj.io/v1alpha1/applications").get("items", [])
for a in apps:
    m, spec, st = a["metadata"], a.get("spec", {}), a.get("status", {})
    name, ns = m["name"], m.get("namespace", "argocd")
    src = spec.get("source", {}) or (spec.get("sources", [{}]) or [{}])[0]
    dest = spec.get("destination", {})
    sync = st.get("sync", {}).get("status", "Unknown")
    health = st.get("health", {}).get("status", "Unknown")
    repo, path, rev = src.get("repoURL", "?"), src.get("path", "?"), src.get("targetRevision", "?")
    dns, dserver = dest.get("namespace", "?"), dest.get("server", "?")
    text = (
        f"ArgoCD Application '{name}' on cluster {CLUSTER} (namespace {ns}). "
        f"Sync status: {sync}. Health status: {health}. "
        f"Source repo: {repo} path: {path} revision: {rev}. "
        f"Destination: server {dserver} namespace {dns}. "
        f"Project: {spec.get('project', 'default')}."
    )
    add(
        f"argocd/app/{name}",
        f"argocd://{CLUSTER}/{ns}/{name}",
        f"ArgoCD App: {name} ({sync}/{health})",
        text,
        {
            "kind": "Application",
            "cluster": CLUSTER,
            "namespace": ns,
            "name": name,
            "sync": sync,
            "health": health,
            "repo": repo,
            "dest_namespace": dns,
            "component_category": "argocd",
        },
    )

# --- Deployments (apps/v1) ---
deps = _get("/apis/apps/v1/deployments").get("items", [])
for d in deps:
    m, spec, st = d["metadata"], d.get("spec", {}), d.get("status", {})
    name, ns = m["name"], m["namespace"]
    containers = spec.get("template", {}).get("spec", {}).get("containers", [])
    images = ", ".join(c.get("image", "?") for c in containers)
    replicas = spec.get("replicas", 0)
    ready = st.get("readyReplicas", 0)
    text = (
        f"Kubernetes Deployment '{name}' in namespace {ns} on cluster {CLUSTER}. "
        f"Replicas: {ready}/{replicas} ready. Images: {images}. "
        f"Containers: {', '.join(c.get('name', '?') for c in containers)}."
    )
    add(
        f"k8s/deployment/{ns}/{name}",
        f"k8s://{CLUSTER}/{ns}/Deployment/{name}",
        f"Deployment: {ns}/{name}",
        text,
        {
            "kind": "Deployment",
            "cluster": CLUSTER,
            "namespace": ns,
            "name": name,
            "replicas": replicas,
            "ready": ready,
            "component_category": "k8s",
        },
    )

# --- Services (core/v1) ---
svcs = _get("/api/v1/services").get("items", [])
for s in svcs:
    m, spec = s["metadata"], s.get("spec", {})
    name, ns = m["name"], m["namespace"]
    ports = ", ".join(f"{p.get('port')}/{p.get('protocol', 'TCP')}" for p in spec.get("ports", []))
    text = (
        f"Kubernetes Service '{name}' in namespace {ns} on cluster {CLUSTER}. "
        f"Type: {spec.get('type', 'ClusterIP')}. Ports: {ports or 'none'}. "
        f"ClusterIP: {spec.get('clusterIP', '?')}."
    )
    add(
        f"k8s/service/{ns}/{name}",
        f"k8s://{CLUSTER}/{ns}/Service/{name}",
        f"Service: {ns}/{name}",
        text,
        {
            "kind": "Service",
            "cluster": CLUSTER,
            "namespace": ns,
            "name": name,
            "component_category": "k8s",
        },
    )

# --- Ingresses (networking.k8s.io/v1) ---
try:
    ings = _get("/apis/networking.k8s.io/v1/ingresses").get("items", [])
except Exception:
    ings = []
for i in ings:
    m, spec = i["metadata"], i.get("spec", {})
    name, ns = m["name"], m["namespace"]
    hosts = ", ".join(r.get("host", "?") for r in spec.get("rules", []))
    text = (
        f"Kubernetes Ingress '{name}' in namespace {ns} on cluster {CLUSTER}. "
        f"Hosts: {hosts or 'none'}. IngressClass: {spec.get('ingressClassName', 'default')}."
    )
    add(
        f"k8s/ingress/{ns}/{name}",
        f"k8s://{CLUSTER}/{ns}/Ingress/{name}",
        f"Ingress: {ns}/{name}",
        text,
        {
            "kind": "Ingress",
            "cluster": CLUSTER,
            "namespace": ns,
            "name": name,
            "component_category": "k8s",
        },
    )

json.dump(docs, open("/tmp/k8s_docs.json", "w"))
by_kind: dict[str, int] = {}
for d in docs:
    by_kind[d["metadata"]["kind"]] = by_kind.get(d["metadata"]["kind"], 0) + 1
print(f"Fetched {len(docs)} documents from {CLUSTER}: {by_kind}")
