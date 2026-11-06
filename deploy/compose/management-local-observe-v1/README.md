# Omniscience local Observe source profile

Layer `compose.yaml` here after the base and source fragments in
`../management-readonly-local/`. Run with the Compose project name
`management-local-observe-v1`. The API listens on the owner read network at
`http://omniscience-api:8000` and on host loopback port `18000`. Admin listens on
host loopback port `13001`. The owner uses separate networks and data volumes
for this profile.

After `/ready` reports healthy, export a generated URL-safe random value of at
least 32 characters as `OMNISCIENCE_LOCAL_OBSERVE_BROKER_TOKEN`. Run the
bootstrap with your assembled profile's Compose arguments.

```sh
docker compose -p management-local-observe-v1 exec -T \
  -e OMNISCIENCE_LOCAL_OBSERVE_BROKER_TOKEN omniscience-api \
  python -m omniscience_server.local_observe
```

The seed is synthetic and can be rerun with the same credential. It creates
these two workspace-local topologies and indexes a topology note and database
runbook for each workspace.

| Workspace | UUID | Blast radius seed |
| --- | --- | --- |
| Alpha | `a34f0802-8321-5c7c-8a6b-dea95391faf5` | `service://observe-alpha-database` |
| Beta | `beac9cbb-b8e1-52da-9fe7-5c5e591825f5` | `service://observe-beta-database` |

The Portal backend receives the same credential through
`KV_OMNISCIENCE_BROKER_ADMIN_CREDENTIAL`. It is a dedicated Observe broker token
with permission to call only `POST /api/v1/tokens/profiles/omniscience-mcp-read-v1`
for these workspaces. Request `name`, `workspace_id`, and a UTC `expires_at`
within 900 seconds to obtain the one-time bearer for that workspace. The
returned token has the exact `search` scope. Authenticate to the MCP transport
at `/mcp/` and call `contract_info` to inspect the published contract.
