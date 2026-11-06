// Command management-readonly-v1-consumer-kit is a read-only CLI wrapper
// around the checks in schema.go/severance.go (AC-SP86-6). It never mutates
// release evidence or calls a network endpoint.
package main

import (
	_ "embed"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"time"
)

//go:embed receipt.schema.json
var receiptSchemaBytes []byte

// kitDir is this source file's own directory, resolved via the compiler-
// recorded call site so invocation cwd does not change file resolution.
var kitDir = func() string {
	_, file, _, _ := runtime.Caller(0)
	return filepath.Dir(file)
}()

func inspectReleaseFiles(releaseRoot string) (releaseFiles, []string) {
	var files releaseFiles
	var err error
	releaseRoot, err = filepath.Abs(releaseRoot)
	if err != nil {
		return files, []string{err.Error()}
	}
	contractsDir := filepath.Join(releaseRoot, "..", "..")
	schemaDigest, err := contractSchemaDigest(contractsDir)
	if err != nil {
		return files, []string{err.Error()}
	}
	files.schemaDigest = schemaDigest
	mcpSchemaDir := filepath.Join(contractsDir, "mcp", "schemas", "v1")
	contractInfoInputPath := filepath.Join(mcpSchemaDir, "contract-info.input.schema.json")
	contractInfoInput, err := LoadSchema(contractInfoInputPath)
	if err != nil {
		return files, []string{err.Error()}
	}
	if contractInfoInput["type"] != "object" {
		return files, []string{"contract-info.input.schema.json: expected object input type"}
	}
	errs := ValidateAgainstSchema(map[string]any{}, contractInfoInput, mcpSchemaDir)

	lockSchemaPath := filepath.Join(releaseRoot, releaseLockSchemaPath)
	lockSchemaBytes, err := os.ReadFile(lockSchemaPath)
	if err != nil {
		return files, append(errs, err.Error())
	}
	files.lockSchema, err = parseSchema(lockSchemaBytes, lockSchemaPath)
	if err != nil {
		return files, append(errs, err.Error())
	}
	errs = append(errs, checkProfileIDConst(files.lockSchema)...)
	files.kitDigest, err = consumerKitDigest(releaseRoot, lockSchemaBytes)
	if err != nil {
		return files, append(errs, err.Error())
	}

	fixturePath := filepath.Join(releaseRoot, severanceFixturePath)
	fixtureBytes, err := os.ReadFile(fixturePath)
	if err != nil {
		return files, append(errs, err.Error())
	}
	fixtures, err := parseSeveranceFixtures(fixtureBytes, fixturePath)
	if err != nil {
		return files, append(errs, err.Error())
	}
	files.fixtureDigest = digest(fixtureBytes)
	return files, append(errs, CheckSeveranceFixtures(fixtures)...)
}

func checkProfileIDConst(releaseLockSchema map[string]any) []string {
	properties, ok := releaseLockSchema["properties"].(map[string]any)
	if !ok {
		return []string{"release-lock.schema.json: missing 'properties'"}
	}
	profileID, ok := properties["profile_id"].(map[string]any)
	if !ok {
		return []string{"release-lock.schema.json: missing 'properties.profile_id'"}
	}
	if profileID["const"] != "management-readonly-v1" {
		return []string{"release-lock.schema.json: profile_id const is not 'management-readonly-v1'"}
	}
	return nil
}

type releaseReceipt struct {
	ReleaseLockDigest string `json:"release_lock_digest"`
	Auth              struct {
		TokenProfile   string   `json:"token_profile"`
		Scopes         []string `json:"scopes"`
		TokenIssuedAt  string   `json:"token_issued_at"`
		TokenExpiresAt string   `json:"token_expires_at"`
	} `json:"auth"`
	ObservedAt      string `json:"observed_at"`
	ExpiresAt       string `json:"expires_at"`
	FreshnessMaxAge int64  `json:"freshness_max_age_seconds"`
}

// checkReceipt validates the current observation against the pinned release.
func checkReceipt(path, releaseLockDigest, authTokenProfile string, now time.Time) []string {
	raw, err := os.ReadFile(path) // #nosec G304 -- explicit local evidence path
	if err != nil {
		return []string{fmt.Sprintf("read receipt %s: %v", path, err)}
	}
	var receipt releaseReceipt
	if err := json.Unmarshal(raw, &receipt); err != nil {
		return []string{fmt.Sprintf("parse receipt %s: %v", path, err)}
	}
	receiptSchema, err := parseSchema(receiptSchemaBytes, "receipt.schema.json")
	if err != nil {
		return []string{err.Error()}
	}
	var receiptValue any
	if err := json.Unmarshal(raw, &receiptValue); err != nil {
		return []string{fmt.Sprintf("parse receipt %s: %v", path, err)}
	}
	errs := ValidateAgainstSchema(receiptValue, receiptSchema, kitDir)
	if receipt.ReleaseLockDigest != releaseLockDigest {
		errs = append(errs, "receipt release_lock_digest does not match the verified release lock")
	}
	errs = append(errs, CheckAuthTokenProfile(authTokenProfile, receipt.Auth.TokenProfile)...)
	if len(receipt.Auth.Scopes) != 1 || receipt.Auth.Scopes[0] != "search" {
		errs = append(errs, "receipt auth scopes must contain exactly [search]")
	}
	tokenIssuedAt, tokenIssuedErr := time.Parse(time.RFC3339, receipt.Auth.TokenIssuedAt)
	tokenExpiresAt, tokenExpiresErr := time.Parse(time.RFC3339, receipt.Auth.TokenExpiresAt)
	if tokenIssuedErr != nil || tokenExpiresErr != nil {
		errs = append(errs, "receipt auth token_issued_at and token_expires_at must be RFC3339 timestamps")
	} else {
		tokenLifetime := tokenExpiresAt.Sub(tokenIssuedAt)
		if tokenLifetime <= 0 || tokenLifetime > 30*24*time.Hour {
			errs = append(errs, "receipt auth token lifetime must be positive and no longer than 30 days")
		}
		if tokenIssuedAt.After(now) || !tokenExpiresAt.After(now) {
			errs = append(errs, "receipt auth token is not currently valid")
		}
	}
	observedAt, observedErr := time.Parse(time.RFC3339, receipt.ObservedAt)
	expiresAt, expiresErr := time.Parse(time.RFC3339, receipt.ExpiresAt)
	if observedErr != nil || expiresErr != nil {
		errs = append(errs, "receipt observed_at and expires_at must be RFC3339 timestamps")
	} else {
		if receipt.FreshnessMaxAge <= 0 || receipt.FreshnessMaxAge > int64((30*24*time.Hour)/time.Second) {
			errs = append(errs, "receipt freshness_max_age_seconds must be positive and no longer than 30 days")
		} else {
			maxAge := time.Duration(receipt.FreshnessMaxAge) * time.Second
			if got := EvaluateFreshness(now.Sub(observedAt), maxAge); got != FreshnessFresh {
				errs = append(errs, fmt.Sprintf("receipt freshness is %s", got))
			}
			if tokenIssuedErr == nil && observedAt.Before(tokenIssuedAt) {
				errs = append(errs, "receipt observed_at predates the auth token")
			}
			if !expiresAt.Equal(observedAt.Add(maxAge)) {
				errs = append(errs, "receipt expires_at must equal observed_at plus freshness_max_age_seconds")
			}
			if !expiresAt.After(now) {
				errs = append(errs, "receipt has expired")
			}
		}
	}
	return errs
}

func main() {
	releaseRoot := flag.String("release-root", filepath.Join(kitDir, ".."), "local management-readonly-v1 release directory")
	lockPath := flag.String("release-lock", "", "producer release lock JSON")
	lockDigest := flag.String("release-lock-digest", "", "independently obtained SHA-256 digest of the producer release lock")
	receiptPath := flag.String("receipt", "", "current consumer observation receipt JSON")
	flag.Parse()

	files, errs := inspectReleaseFiles(*releaseRoot)
	if len(errs) != 0 {
		reportFailure(errs)
	}
	if *receiptPath == "" || *lockPath == "" || *lockDigest == "" {
		reportFailure([]string{"PENDING: --receipt, --release-lock and --release-lock-digest are required for receipt verification"})
	}
	lock, errs := checkReleaseLock(*lockPath, *lockDigest, files)
	if len(errs) == 0 {
		errs = checkReceipt(*receiptPath, *lockDigest, lock.AuthTokenProfile, time.Now())
	}
	if len(errs) != 0 {
		reportFailure(errs)
	}
	if len(lock.Pending) != 0 {
		fmt.Fprintln(os.Stderr, "management-readonly-v1-consumer-kit: consumer evidence verified; PENDING producer release qualification")
		os.Exit(1)
	}
	fmt.Println("management-readonly-v1-consumer-kit: consumer evidence verified")
}

func reportFailure(errs []string) {
	fmt.Fprintln(os.Stderr, "management-readonly-v1-consumer-kit verification: RED (decision-return)")
	for _, e := range errs {
		fmt.Fprintln(os.Stderr, "-", e)
	}
	os.Exit(1)
}
