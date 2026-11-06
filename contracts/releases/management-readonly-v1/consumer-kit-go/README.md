# management-readonly-v1 Go consumer kit (AC-SP86-6)

This Go module verifies the `management-readonly-v1` consumer contract and
severance fixtures using the standard library (ADR-0022 OMR-9).

```text
consumer-kit-go/
  go.mod          zero third-party dependencies
  schema.go       dependency-free JSON Schema subset validator (mirrors
                  contracts/mcp/validator/schema_check.py's vocabulary:
                  type/required/properties/additionalProperties/enum/const/
                  pattern/minLength/minItems/items/$ref)
  severance.go    loads ../severance-fixtures.json and verifies the complete
                  fail-closed matrix
  release.go      rehashes the released contract entries and verifies the
                  producer release lock against an independent digest pin
  receipt.schema.json versioned consumer receipt shape (v1)
  main.go         read-only CLI for the pinned release and current receipt
  main_test.go    the actual verifier (`go test ./...`)
```

## Running the probe

```bash
cd contracts/releases/management-readonly-v1/consumer-kit-go
go vet ./...
build_dir="$(mktemp -d)"
go build -o "$build_dir/management-readonly-v1-consumer-kit" ./...
go test ./...
rm "$build_dir/management-readonly-v1-consumer-kit"
rmdir "$build_dir"
```

The Go checks run offline. To verify a current consumer observation, supply the
producer release lock, its SHA-256 digest from a trusted independent release
record, and the observation receipt.

```bash
go run . \
  --release-lock /path/to/release-lock.json \
  --release-lock-digest "$TRUSTED_RELEASE_LOCK_DIGEST" \
  --receipt /path/to/receipt.json
```

Use `--release-root` to select another checkout's
`contracts/releases/management-readonly-v1` directory. The directory must sit
alongside the released contract trees in their original layout.

The verifier hashes the supplied lock's exact bytes, validates it against the
released lock schema, and re-derives its `schema_set_digest`,
`severance_fixture_digest`, and `language_neutral_consumer_kit_digest` from
local files. It requires a clean GREEN producer check and validates all seven
severance cases by ID, scenario, reason, and decision. The current v1 producer
lock records the OCI registry digest as pending. The CLI reports pending
producer qualification with a nonzero exit status even when the consumer
evidence verifies.

`receipt.schema.json` defines the observation format. Its
`release_lock_digest` binds the observation to the pinned lock. Its `auth`
object records the `omniscience-mcp-read-v1` profile, the single `search`
scope, and the issue and expiry timestamps of a currently valid token with
a maximum lifetime of 30 days. The observation must fall at or after token issue
and within the declared freshness window. The CLI checks these receipt
claims locally. Obtain observations from the authorized consumer probe.
The Go tests use synthetic observations with a fixed clock.

`scripts/qualify_management_readonly_release.py`
folds this directory and `../schemas/v1/release-lock.schema.json` into the
lock's `language_neutral_consumer_kit_digest`. The default Go executable names
are excluded from that digest. The release workflow runs the Go toolchain
checks in a separate job.
