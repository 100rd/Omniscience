# Runbook — AWS ingestion from a local (laptop) Omniscience

How to exercise the AWS connector against real AWS from the **local docker-compose
stack** — no in-cluster deploy, no IRSA. This is the dev/PoC path of
[ADR-0014](../decisions/0014-aws-dual-mechanism-ingestion.md); production runs
in-cluster with IRSA (separate).

## How the laptop differs from production

The connector obtains AWS credentials from the source's `secrets_ref` via the
env-based `SecretsResolver` — it does **not** require an in-cluster identity. On
a laptop you supply credentials directly; **no STS AssumeRole is involved** (the
connector does not assume roles yet — direct creds with a read policy are used).

| | Laptop (this runbook) | Production (later) |
|---|---|---|
| Identity | your creds / a dedicated IAM user | EKS IRSA role |
| Creds path | direct keys in `.env` | role on the ServiceAccount |
| IAM source | `enable_local_dev` policy (infra PR #263) | `enable_config_reader_role` |

## Step 1 — Get read-only AWS credentials

Pick one:

- **Your own AWS SSO/CLI creds** — simplest if your permission set already has
  read access (or attach the managed policy below to it).
- **Dedicated IAM user** — apply the Omniscience IaC module
  (`qbiq-ai/infra` PR #263) with `enable_local_dev = true` and
  `create_local_dev_user = true`; it creates `omniscience-local-dev` with the
  read-only policy. Create an access key out-of-band:
  ```sh
  aws iam create-access-key --user-name omniscience-local-dev --profile log-archive
  ```
  (Keys are intentionally NOT stored in Terraform state.)

The read policy grants only: `config:*Aggregate*`/`Describe*` + `s3:GetObject`/
`ListBucket` on the Config bucket (config mode), and read-only `describe`/`list`
on EC2/S3/IAM (describe mode). Read-only — nothing mutating.

## Step 2 — Give the creds to the local stack

The `app` service reads `.env`. Add (prefix `OMNI_AWS_`; the `env:OMNI_AWS_`
secrets_ref strips the prefix → `aws_access_key_id`, …):

```dotenv
# .env  (NOT committed — .env is gitignored)
OMNI_AWS_aws_access_key_id=AKIA...
OMNI_AWS_aws_secret_access_key=...
# OMNI_AWS_aws_session_token=...   # only if using SSO/temporary creds
```

Recreate the app so it picks up the env:
```sh
docker compose up -d app
```

## Step 3 — Create an AWS source

Mint a workspace-scoped token with `sources:write` (see the admin UI or
`POST /api/v1/tokens`), then:

### Path A — `describe` mode (works TODAY, returns real data)

Best for an immediate end-to-end smoke test — ingests real EC2/S3/IAM from the
account your creds belong to, exercising entities + graph + `blast_radius` +
search locally.

```sh
curl -X POST http://localhost:8000/api/v1/sources \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{
    "type": "aws",
    "name": "aws-laptop-describe",
    "config": { "acquisition": "describe", "regions": ["eu-west-1"],
                "services": ["s3","iam","ec2"] },
    "secrets_ref": "env:OMNI_AWS_"
  }'
```

### Path B — `config` mode (ready, but 0 docs until infra #160)

Same creds; reads the org Config aggregator. The aggregator in Log Archive is
**not deployed yet (infra #160)**, so aggregate calls return
`NoSuchConfigurationAggregator` and the connector **gracefully yields 0 documents**
(per-type skip, no crash). Flip to this once #160 ships.

```sh
curl -X POST http://localhost:8000/api/v1/sources \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{
    "type": "aws",
    "name": "aws-laptop-config",
    "config": { "acquisition": "config",
                "aggregator_name": "<org-aggregator-name>",
                "config_regions": ["eu-west-1"],
                "config_resource_types": ["AWS::EC2::VPC","AWS::EC2::SecurityGroup",
                  "AWS::IAM::Role","AWS::S3::Bucket"] },
    "secrets_ref": "env:OMNI_AWS_"
  }'
```

## Step 4 — Sync and verify

```sh
curl -X POST "http://localhost:8000/api/v1/sources/<source_id>/sync" \
  -H "Authorization: Bearer $TOKEN"

docker compose logs app --since 2m | grep -iE "discovery_sync_complete|aws"
```

Path A: expect entities for your real AWS resources + relationship edges; try
`blast_radius` / `get_related_entities` and a search. Path B (pre-#160): expect
`total=0` with per-type skip warnings — that confirms auth + the connector path
work; real data arrives once the aggregator exists.

## Teardown

- Local: delete the source (`DELETE /api/v1/sources/<id>`), remove the
  `OMNI_AWS_*` lines from `.env`, `docker compose up -d app`.
- IAM (if you applied local-dev): delete the access key, then
  `terragrunt destroy` the single `log-archive/eu-west-1/omniscience/` unit —
  see `modules/omniscience/README.md` for the one-shot removal of the entire
  Omniscience footprint.
