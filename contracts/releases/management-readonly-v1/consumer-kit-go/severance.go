package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"
	"time"
)

// requiredSeveranceCases pins the seven AC-SP86-4 cases and their exact
// typed fallbacks. The fixture file alone cannot define its own coverage.
var requiredSeveranceCases = map[string]struct{ scenario, reason string }{
	"sev-loss-001":             {"loss", "producer_unavailable"},
	"sev-loss-002":             {"loss", "source_severed"},
	"sev-token-expiry-001":     {"token_expiry", "token_revoked"},
	"sev-freshness-expiry-001": {"freshness_expiry", "evidence_stale"},
	"sev-contract-skew-001":    {"contract_skew", "contract_major_mismatch"},
	"sev-contract-skew-002":    {"contract_skew", "schema_digest_mismatch"},
	"sev-contract-skew-003":    {"contract_skew", "producer_manifest_mismatch"},
}

// SeveranceFixture is one entry of
// contracts/releases/management-readonly-v1/severance-fixtures.json.
type SeveranceFixture struct {
	ID                string   `json:"id"`
	Scenario          string   `json:"scenario"`
	Description       string   `json:"description"`
	Reason            string   `json:"reason"`
	ExpectedDecision  string   `json:"expected_decision"`
	MustNotValidateAs []string `json:"must_not_validate_as"`
}

type severanceFixtureFile struct {
	Schema   string             `json:"schema"`
	Note     string             `json:"note"`
	Fixtures []SeveranceFixture `json:"fixtures"`
}

// LoadSeveranceFixtures reads the AC-SP86-4 severance fixture matrix.
func LoadSeveranceFixtures(path string) ([]SeveranceFixture, error) {
	raw, err := os.ReadFile(path) // #nosec G304 -- caller-controlled fixture path, not user input
	if err != nil {
		return nil, fmt.Errorf("read severance fixtures %s: %w", path, err)
	}
	return parseSeveranceFixtures(raw, path)
}

func parseSeveranceFixtures(raw []byte, path string) ([]SeveranceFixture, error) {
	var parsed severanceFixtureFile
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&parsed); err != nil {
		return nil, fmt.Errorf("parse severance fixtures %s: %w", path, err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		return nil, fmt.Errorf("parse severance fixtures %s: unexpected trailing JSON", path)
	}
	if parsed.Schema != "management-readonly-v1-severance-fixture-matrix-v1" {
		return nil, fmt.Errorf("severance fixtures %s: unsupported schema %q", path, parsed.Schema)
	}
	if strings.TrimSpace(parsed.Note) == "" {
		return nil, fmt.Errorf("severance fixtures %s: missing note", path)
	}
	return parsed.Fixtures, nil
}

// CheckSeveranceFixture returns one error string per violation (empty means
// the fixture is a well-formed, fail-closed severance scenario). Every
// scenario in the SP-86 kit must require a typed fallback -- never claim
// healthy/empty/cached-current -- so ExpectedDecision must be exactly
// "fallback_required" and Reason must match the specific case.
func CheckSeveranceFixture(f SeveranceFixture) []string {
	var errs []string
	if expected, ok := requiredSeveranceCases[f.ID]; !ok {
		errs = append(errs, fmt.Sprintf("unknown severance fixture id %q", f.ID))
	} else {
		if f.Scenario != expected.scenario {
			errs = append(errs, fmt.Sprintf("%s: expected scenario %q, got %q", f.ID, expected.scenario, f.Scenario))
		}
		if f.Reason != expected.reason {
			errs = append(errs, fmt.Sprintf("%s: expected reason %q, got %q", f.ID, expected.reason, f.Reason))
		}
	}
	if strings.TrimSpace(f.Description) == "" {
		errs = append(errs, fmt.Sprintf("%s: missing description", f.ID))
	}
	if f.ExpectedDecision != "fallback_required" {
		errs = append(errs, fmt.Sprintf(
			"%s: expected_decision must be 'fallback_required', got %q", f.ID, f.ExpectedDecision,
		))
	}
	forbidden := map[string]bool{"healthy": true, "empty": true, "cached_current": true}
	seen := make(map[string]bool, len(f.MustNotValidateAs))
	for _, claim := range f.MustNotValidateAs {
		if !forbidden[claim] {
			errs = append(errs, fmt.Sprintf("%s: unrecognized must_not_validate_as claim %q", f.ID, claim))
			continue
		}
		if seen[claim] {
			errs = append(errs, fmt.Sprintf("%s: duplicate must_not_validate_as claim %q", f.ID, claim))
		}
		seen[claim] = true
	}
	for claim := range forbidden {
		if !seen[claim] {
			errs = append(errs, fmt.Sprintf("%s: missing must_not_validate_as claim %q", f.ID, claim))
		}
	}
	return errs
}

// CheckSeveranceFixtures validates matrix-level properties that a row checker
// cannot observe, particularly unique stable IDs and complete coverage.
func CheckSeveranceFixtures(fixtures []SeveranceFixture) []string {
	if len(fixtures) == 0 {
		return []string{"severance-fixtures.json: no fixtures found"}
	}
	seen := make(map[string]bool, len(fixtures))
	var errs []string
	for _, fixture := range fixtures {
		if seen[fixture.ID] {
			errs = append(errs, fmt.Sprintf("severance fixture duplicate id %q", fixture.ID))
		}
		seen[fixture.ID] = true
		errs = append(errs, CheckSeveranceFixture(fixture)...)
	}
	for id := range requiredSeveranceCases {
		if !seen[id] {
			errs = append(errs, fmt.Sprintf("severance fixtures missing required case %q", id))
		}
	}
	return errs
}

// CheckAuthTokenProfile verifies an observed token profile id against the
// REQ-MCP-8 profile declared by the receipt's governing contract.
func CheckAuthTokenProfile(expected, observed string) []string {
	if strings.TrimSpace(expected) == "" {
		return []string{"auth token profile fixture is empty"}
	}
	if observed != expected {
		return []string{fmt.Sprintf("auth token profile mismatch: expected %q, got %q", expected, observed)}
	}
	return nil
}

// FreshnessStatus is the closed REQ-MCP-5 enum this kit can derive.
type FreshnessStatus string

const (
	FreshnessFresh   FreshnessStatus = "fresh"
	FreshnessStale   FreshnessStatus = "stale"
	FreshnessUnknown FreshnessStatus = "unknown"
)

// EvaluateFreshness mirrors REQ-MCP-5's conservative freshness derivation: a
// negative age is unknown, and an age beyond the maximum is stale.
// Retain subsecond precision so a future observation remains unknown.
func EvaluateFreshness(age, maxAge time.Duration) FreshnessStatus {
	if age < 0 {
		return FreshnessUnknown
	}
	if age > maxAge {
		return FreshnessStale
	}
	return FreshnessFresh
}
