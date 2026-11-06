package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// TestConsumerKitVerifiesCleanRelease is the actual read-only AC-SP86-6
// verifier: `go test ./...` from this directory validates the released
// schemas, severance fixtures, auth profile, and freshness derivation
// without executing Node.js or a TypeScript-only generated binding.
func TestConsumerKitVerifiesCleanRelease(t *testing.T) {
	_, errs := inspectReleaseFiles(filepath.Join(kitDir, ".."))
	for _, e := range errs {
		t.Error(e)
	}
}

func TestConsumerKitAcceptsRelativeReleaseRoot(t *testing.T) {
	absolute, errs := inspectReleaseFiles(filepath.Join(kitDir, ".."))
	if len(errs) != 0 {
		t.Fatal(errs)
	}
	relative, errs := inspectReleaseFiles("..")
	if len(errs) != 0 || relative.schemaDigest != absolute.schemaDigest || relative.kitDigest != absolute.kitDigest || relative.fixtureDigest != absolute.fixtureDigest {
		t.Fatalf("relative release root changed evidence: %v", errs)
	}
}

func TestCheckSeveranceFixtureRejectsWrongDecision(t *testing.T) {
	bad := loadReleasedFixtures(t)[0]
	bad.ExpectedDecision = "healthy"
	errs := CheckSeveranceFixture(bad)
	if len(errs) == 0 {
		t.Fatal("expected CheckSeveranceFixture to reject a non-fallback_required decision")
	}
}

func TestCheckSeveranceFixtureRejectsUnknownReason(t *testing.T) {
	bad := loadReleasedFixtures(t)[0]
	bad.Reason = "not_a_real_reason"
	errs := CheckSeveranceFixture(bad)
	if len(errs) == 0 {
		t.Fatal("expected CheckSeveranceFixture to reject an unrecognized reason")
	}
}

func TestCheckAuthTokenProfileRejectsForeignProfile(t *testing.T) {
	errs := CheckAuthTokenProfile("omniscience-mcp-read-v1", "some-other-profile")
	if len(errs) == 0 {
		t.Fatal("expected CheckAuthTokenProfile to reject a non-pinned profile id")
	}
}

func TestSeveranceInputTamperFailsClosed(t *testing.T) {
	path := filepath.Join(kitDir, "..", "severance-fixtures.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	tampered := strings.Replace(string(raw), `"must_not_validate_as": ["healthy", "empty", "cached_current"]`, `"must_not_validate_as": ["healthy", "empty"]`, 1)
	if tampered == string(raw) {
		t.Fatal("fixture row was not changed")
	}
	tempPath := filepath.Join(t.TempDir(), "severance-fixtures.json")
	if err := os.WriteFile(tempPath, []byte(tampered), 0o600); err != nil {
		t.Fatal(err)
	}
	fixtures, err := LoadSeveranceFixtures(tempPath)
	if err != nil {
		t.Fatal(err)
	}
	if errs := CheckSeveranceFixtures(fixtures); len(errs) == 0 {
		t.Fatal("tampered actual fixture input unexpectedly passed")
	}
}

func TestSeveranceMatrixRejectsDuplicateIDs(t *testing.T) {
	fixtures := loadReleasedFixtures(t)
	fixtures = append(fixtures, fixtures[0])
	if errs := CheckSeveranceFixtures(fixtures); len(errs) != 1 || !strings.Contains(errs[0], "duplicate id") {
		t.Fatalf("expected one duplicate-ID error, got %v", errs)
	}
}

func TestSeveranceMatrixRejectsMissingCasesAndSwappedTypedFallback(t *testing.T) {
	fixtures := loadReleasedFixtures(t)
	var partial []SeveranceFixture
	for _, fixture := range fixtures {
		switch fixture.ID {
		case "sev-loss-001", "sev-contract-skew-002", "sev-contract-skew-003":
			continue
		case "sev-token-expiry-001":
			fixture.Reason = "evidence_stale"
		}
		partial = append(partial, fixture)
	}
	got := strings.Join(CheckSeveranceFixtures(partial), "\n")
	for _, want := range []string{
		`missing required case "sev-loss-001"`,
		`missing required case "sev-contract-skew-002"`,
		`missing required case "sev-contract-skew-003"`,
		`sev-token-expiry-001: expected reason "token_revoked"`,
	} {
		if !strings.Contains(got, want) {
			t.Errorf("expected %q, got %q", want, got)
		}
	}
}

func loadReleasedFixtures(t *testing.T) []SeveranceFixture {
	t.Helper()
	fixtures, err := LoadSeveranceFixtures(filepath.Join(kitDir, "..", severanceFixturePath))
	if err != nil {
		t.Fatal(err)
	}
	return fixtures
}

func TestReceiptRequiresValidAuthScopeAndFreshness(t *testing.T) {
	now := time.Date(2026, 11, 6, 12, 0, 0, 0, time.UTC)
	lockDigest := digest([]byte("test-only release lock"))
	receipt := map[string]any{
		"schema":              "management-readonly-v1-consumer-receipt-v1",
		"release_lock_digest": lockDigest,
		"auth": map[string]any{
			"token_profile": "omniscience-mcp-read-v1", "scopes": []string{"search"},
			"token_issued_at":  now.Add(-time.Minute).Format(time.RFC3339),
			"token_expires_at": now.Add(time.Hour).Format(time.RFC3339),
		},
		"observed_at":               now.Add(-30 * time.Second).Format(time.RFC3339),
		"expires_at":                now.Add(time.Hour - 30*time.Second).Format(time.RFC3339),
		"freshness_max_age_seconds": 3600,
	}
	path := filepath.Join(t.TempDir(), "receipt.json")
	writeReceipt := func() {
		t.Helper()
		raw, err := json.Marshal(receipt)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, raw, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	writeReceipt()
	if errs := checkReceipt(path, lockDigest, tokenProfile, now); len(errs) != 0 {
		t.Fatalf("valid receipt rejected: %v", errs)
	}
	if errs := checkReceipt(path, digest([]byte("different lock")), tokenProfile, now); len(errs) != 1 || !strings.Contains(errs[0], "release_lock_digest") {
		t.Fatalf("expected mismatched release lock error, got %v", errs)
	}

	auth := receipt["auth"].(map[string]any)
	auth["scopes"] = []string{"search", "admin"}
	writeReceipt()
	if errs := checkReceipt(path, lockDigest, tokenProfile, now); len(errs) == 0 {
		t.Fatal("receipt with extra auth scope unexpectedly passed")
	}

	auth["scopes"] = []string{"search"}
	receipt["observed_at"] = now.Add(-2 * time.Hour).Format(time.RFC3339)
	receipt["expires_at"] = now.Add(-time.Hour).Format(time.RFC3339)
	writeReceipt()
	if errs := checkReceipt(path, lockDigest, tokenProfile, now); len(errs) == 0 {
		t.Fatal("stale receipt unexpectedly passed")
	}

	observedAt := now.Add(500 * time.Millisecond)
	receipt["observed_at"] = observedAt.Format(time.RFC3339Nano)
	receipt["expires_at"] = observedAt.Add(time.Hour).Format(time.RFC3339Nano)
	writeReceipt()
	if errs := checkReceipt(path, lockDigest, tokenProfile, now); len(errs) != 1 || errs[0] != "receipt freshness is unknown" {
		t.Fatalf("expected subsecond future observation to be unknown, got %v", errs)
	}
}

func TestEvaluateFreshnessNeverOptimisticOnUnmeasuredAge(t *testing.T) {
	if got := EvaluateFreshness(-5*time.Second, 300*time.Second); got != FreshnessUnknown {
		t.Fatalf("negative age must be unknown, got %s", got)
	}
}

func TestLoadSchemaRejectsNull(t *testing.T) {
	path := filepath.Join(t.TempDir(), "schema.json")
	if err := os.WriteFile(path, []byte("null"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadSchema(path); err == nil {
		t.Fatal("null schema unexpectedly loaded")
	}
}

func TestValidateAgainstSchemaRejectsMissingRequiredField(t *testing.T) {
	schema := map[string]any{
		"type":                 "object",
		"required":             []any{"profile_id"},
		"additionalProperties": false,
		"properties": map[string]any{
			"profile_id": map[string]any{"type": "string"},
		},
	}
	errs := ValidateAgainstSchema(map[string]any{}, schema, ".")
	if len(errs) == 0 {
		t.Fatal("expected a missing required field to be reported")
	}
}
