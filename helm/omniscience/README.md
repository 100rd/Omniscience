# Omniscience Helm Chart

Helm chart for deploying Omniscience on Kubernetes.

## Prerequisites

- Kubernetes 1.25+
- Helm 3.10+
- A Postgres 14+ instance with pgvector (built-in or external)
- A NATS 2.x instance (built-in or external)

## Install

```bash
helm upgrade --install omniscience ./helm/omniscience \
  --namespace omniscience \
  --create-namespace \
  --set secrets.postgresPassword=changeme \
  --set secrets.apiToken=mytoken
```

## Values Reference

### Image

| Key | Default | Description |
|-----|---------|-------------|
| `image.repository` | `ghcr.io/100rd/omniscience` | Container image repository |
| `image.tag` | `latest` | Image tag (override with Git SHA in CI) |
| `image.pullPolicy` | `IfNotPresent` | Pull policy |

### Application config

| Key | Default | Description |
|-----|---------|-------------|
| `config.logLevel` | `INFO` | Log level (DEBUG, INFO, WARNING, ERROR) |
| `config.embeddingsProvider` | `ollama` | Embeddings backend: `ollama`, `openai`, `voyage` |
| `config.embeddingModel` | `nomic-embed-text` | Embedding model name |
| `config.mcpTransport` | `streamable-http` | MCP transport: `stdio` or `streamable-http` |
| `config.freshnessSloSeconds` | `3600` | Default freshness SLO in seconds |
| `config.ollamaUrl` | `""` | Ollama endpoint (required when provider=ollama) |
| `config.openaiBaseUrl` | `""` | Optional OpenAI base URL override |
| `config.corsOrigins` | `""` | Comma-separated CORS origins |

### Secrets

| Key | Default | Description |
|-----|---------|-------------|
| `secrets.postgresPassword` | `""` | **Required.** PostgreSQL password |
| `secrets.apiToken` | `""` | Bearer token for REST + MCP auth |
| `secrets.openaiApiKey` | `""` | OpenAI or Voyage API key |

### PostgreSQL (built-in)

| Key | Default | Description |
|-----|---------|-------------|
| `postgres.enabled` | `true` | Deploy bundled Postgres |
| `postgres.image` | `pgvector/pgvector:pg16` | Postgres image |
| `postgres.database` | `omniscience` | Database name |
| `postgres.user` | `omniscience` | Database user |
| `postgres.storageSize` | `20Gi` | PVC size |
| `postgres.storageClass` | `""` | StorageClass (empty = cluster default) |
| `postgres.externalUrl` | `""` | Use external DB; disables built-in Postgres |

### NATS (built-in)

| Key | Default | Description |
|-----|---------|-------------|
| `nats.enabled` | `true` | Deploy bundled NATS |
| `nats.image` | `nats:2.10-alpine` | NATS image |
| `nats.storageSize` | `5Gi` | PVC size |
| `nats.storageClass` | `""` | StorageClass (empty = cluster default) |
| `nats.externalUrl` | `""` | Use external NATS; disables built-in NATS |

### Service

| Key | Default | Description |
|-----|---------|-------------|
| `service.type` | `ClusterIP` | Service type |
| `service.port` | `8000` | Service port |

### Ingress

| Key | Default | Description |
|-----|---------|-------------|
| `ingress.enabled` | `false` | Enable Ingress |
| `ingress.className` | `""` | IngressClass name |
| `ingress.annotations` | `{}` | Ingress annotations |
| `ingress.hosts` | see values | Host rules |
| `ingress.tls` | `[]` | TLS configuration |

### Resources

| Key | Default | Description |
|-----|---------|-------------|
| `resources.requests.cpu` | `250m` | CPU request |
| `resources.requests.memory` | `512Mi` | Memory request |
| `resources.limits.cpu` | `1000m` | CPU limit |
| `resources.limits.memory` | `1Gi` | Memory limit |

## Using an External Database

To connect to RDS, CloudSQL, Neon, or any external Postgres:

```bash
helm upgrade --install omniscience ./helm/omniscience \
  --set postgres.enabled=false \
  --set postgres.externalUrl="postgresql://user:pass@host:5432/omniscience" \
  --set secrets.postgresPassword=notused
```

## Using an External NATS Cluster

```bash
helm upgrade --install omniscience ./helm/omniscience \
  --set nats.enabled=false \
  --set nats.externalUrl="nats://nats.example.com:4222"
```

## Enabling Ingress with TLS

```bash
helm upgrade --install omniscience ./helm/omniscience \
  --set ingress.enabled=true \
  --set ingress.className=nginx \
  --set "ingress.hosts[0].host=omniscience.example.com" \
  --set "ingress.hosts[0].paths[0].path=/" \
  --set "ingress.hosts[0].paths[0].pathType=Prefix" \
  --set "ingress.tls[0].secretName=omniscience-tls" \
  --set "ingress.tls[0].hosts[0]=omniscience.example.com"
```
