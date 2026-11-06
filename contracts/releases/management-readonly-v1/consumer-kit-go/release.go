package main

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

const (
	releaseLockSchemaPath = "schemas/v1/release-lock.schema.json"
	severanceFixturePath  = "severance-fixtures.json"
	tokenProfile          = "omniscience-mcp-read-v1"
)

type releaseLock struct {
	Status                 string                     `json:"status"`
	Reasons                []string                   `json:"reasons"`
	WorktreeScopeClean     bool                       `json:"git_worktree_scope_clean"`
	AuthTokenProfile       string                     `json:"auth_token_profile"`
	SchemaSetDigest        string                     `json:"schema_set_digest"`
	SeveranceFixtureDigest string                     `json:"severance_fixture_digest"`
	ConsumerKitDigest      string                     `json:"language_neutral_consumer_kit_digest"`
	Pending                map[string]json.RawMessage `json:"pending"`
}

type releaseFiles struct {
	lockSchema    map[string]any
	schemaDigest  string
	fixtureDigest string
	kitDigest     string
}

func digest(data []byte) string {
	return fmt.Sprintf("sha256:%x", sha256.Sum256(data))
}

func digestRow(path string, data []byte) string {
	return path + ":" + strings.TrimPrefix(digest(data), "sha256:") + "\n"
}

func compositeDigest(rows []string) string {
	sort.Strings(rows)
	return digest([]byte(strings.Join(rows, "")))
}

// contractSchemaDigest mirrors the producer's namespaced schema_set_digest
// construction after checking the bytes of every released pin entry.
func contractSchemaDigest(contractsDir string) (string, error) {
	type pinEntry struct {
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	}
	var rows []string
	for _, name := range []string{"mcp", "management", "pii"} {
		dir := filepath.Join(contractsDir, name)
		raw, err := os.ReadFile(filepath.Join(dir, "pin.json"))
		if err != nil {
			return "", err
		}
		var pin struct {
			Vendored []pinEntry `json:"vendoredEntries"`
			Owned    []pinEntry `json:"ownedEntries"`
		}
		if err := json.Unmarshal(raw, &pin); err != nil {
			return "", fmt.Errorf("parse %s/pin.json: %w", name, err)
		}
		entries := pin.Vendored
		if name == "management" {
			entries = pin.Owned
		}
		if len(entries) == 0 {
			return "", fmt.Errorf("%s/pin.json has no released entries", name)
		}
		seen := make(map[string]bool, len(entries))
		for _, entry := range entries {
			if !fs.ValidPath(entry.Path) || entry.Path == "." || strings.ContainsAny(entry.Path, "\\:\n\r") {
				return "", fmt.Errorf("%s/pin.json has invalid entry path %q", name, entry.Path)
			}
			if seen[entry.Path] {
				return "", fmt.Errorf("%s/pin.json has duplicate entry %q", name, entry.Path)
			}
			seen[entry.Path] = true
			path := filepath.Join(dir, filepath.FromSlash(entry.Path))
			data, err := os.ReadFile(path)
			if err != nil {
				return "", err
			}
			if digest(data) != "sha256:"+entry.SHA256 {
				return "", fmt.Errorf("released contract entry digest mismatch: %s/%s", name, entry.Path)
			}
			if strings.HasSuffix(entry.Path, ".schema.json") {
				if _, err := parseSchema(data, path); err != nil {
					return "", err
				}
			}
			rows = append(rows, digestRow(name+"/"+entry.Path, data))
		}
	}
	return compositeDigest(rows), nil
}

// consumerKitDigest mirrors the producer's kit digest over repo-relative
// paths, including the schema that governs the release lock.
func consumerKitDigest(releaseRoot string, lockSchemaBytes []byte) (string, error) {
	kitRoot := filepath.Join(releaseRoot, "consumer-kit-go")
	repoRoot := filepath.Join(releaseRoot, "..", "..", "..")
	lockSchemaRel, err := filepath.Rel(repoRoot, filepath.Join(releaseRoot, releaseLockSchemaPath))
	if err != nil {
		return "", err
	}
	rows := []string{digestRow(filepath.ToSlash(lockSchemaRel), lockSchemaBytes)}
	err = filepath.WalkDir(kitRoot, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.IsDir() {
			return nil
		}
		if path == filepath.Join(kitRoot, "management-readonly-v1-consumer-kit") ||
			path == filepath.Join(kitRoot, "management-readonly-v1-consumer-kit.exe") {
			return nil
		}
		if !entry.Type().IsRegular() {
			return fmt.Errorf("consumer kit entry is not a regular file: %s", path)
		}
		rel, err := filepath.Rel(repoRoot, path)
		if err != nil {
			return err
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		rows = append(rows, digestRow(filepath.ToSlash(rel), data))
		return nil
	})
	if err != nil {
		return "", err
	}
	if len(rows) == 1 {
		return "", fmt.Errorf("consumer kit directory is empty: %s", kitRoot)
	}
	return compositeDigest(rows), nil
}

func checkReleaseLock(path, pinnedDigest string, files releaseFiles) (releaseLock, []string) {
	var lock releaseLock
	raw, err := os.ReadFile(path)
	if err != nil {
		return lock, []string{fmt.Sprintf("read release lock %s: %v", path, err)}
	}
	if digest(raw) != pinnedDigest {
		return lock, []string{"release lock digest does not match the independently pinned digest"}
	}
	if err := json.Unmarshal(raw, &lock); err != nil {
		return lock, []string{fmt.Sprintf("parse release lock %s: %v", path, err)}
	}

	var errs []string
	for _, binding := range []struct{ field, expected, actual string }{
		{"schema_set_digest", lock.SchemaSetDigest, files.schemaDigest},
		{"severance_fixture_digest", lock.SeveranceFixtureDigest, files.fixtureDigest},
		{"language_neutral_consumer_kit_digest", lock.ConsumerKitDigest, files.kitDigest},
	} {
		if binding.expected != binding.actual {
			errs = append(errs, fmt.Sprintf("release lock %s does not match local release files", binding.field))
		}
	}
	if len(errs) != 0 {
		return lock, errs
	}
	var instance any
	if err := json.Unmarshal(raw, &instance); err != nil {
		return lock, []string{fmt.Sprintf("parse release lock %s: %v", path, err)}
	}
	errs = ValidateAgainstSchema(instance, files.lockSchema, filepath.Dir(path))
	if lock.Status != "GREEN" || !lock.WorktreeScopeClean || len(lock.Reasons) != 0 {
		errs = append(errs, "release lock does not record clean GREEN producer checks")
	}
	if lock.AuthTokenProfile != tokenProfile {
		errs = append(errs, "release lock has the wrong auth token profile")
	}
	return lock, errs
}
