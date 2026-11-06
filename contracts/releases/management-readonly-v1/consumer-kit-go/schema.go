// Package main implements a language-neutral verifier for the
// management-readonly-v1 release (task-sp-86-management-readonly-release,
// AC-SP86-6). It is a plain Go module with zero third-party dependencies
// (see go.mod) so a consumer can validate the released contracts, schemas,
// and severance fixtures without executing Node.js or depending on a
// TypeScript-only generated binding (ADR-0022 OMR-9).
//
// ValidateAgainstSchema below implements the SAME conservative JSON Schema
// subset as contracts/mcp/validator/schema_check.py and
// contracts/management/validator/schema_check.py: type, required,
// properties, additionalProperties, enum, const, pattern, minLength,
// minItems, items, and a same-directory $ref. It is intentionally not a
// general-purpose JSON Schema implementation -- keywords outside that
// subset (e.g. "minimum", "format") are structural no-ops here, exactly as
// in the Python vendored validator.
package main

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// LoadSchema reads and parses one JSON Schema document from disk.
func LoadSchema(path string) (map[string]any, error) {
	raw, err := os.ReadFile(path) // #nosec G304 -- caller-controlled fixture/schema path, not user input
	if err != nil {
		return nil, fmt.Errorf("read schema %s: %w", path, err)
	}
	return parseSchema(raw, path)
}

func parseSchema(raw []byte, path string) (map[string]any, error) {
	var schema map[string]any
	if err := json.Unmarshal(raw, &schema); err != nil {
		return nil, fmt.Errorf("parse schema %s: %w", path, err)
	}
	if schema == nil {
		return nil, fmt.Errorf("parse schema %s: expected JSON object", path)
	}
	return schema, nil
}

// ValidateAgainstSchema validates instance against schema, resolving a
// same-directory "$ref" against schemaDir. Returns one error string per
// violation (nil/empty means valid) -- never panics on a malformed schema
// or instance.
func ValidateAgainstSchema(instance any, schema map[string]any, schemaDir string) []string {
	return validateAt(instance, schema, schemaDir, "$")
}

func validateAt(instance any, schema map[string]any, schemaDir, path string) []string {
	var errs []string

	schema, err := resolveSchema(schema, schemaDir)
	if err != nil {
		return []string{fmt.Sprintf("%s: %v", path, err)}
	}

	if constVal, ok := schema["const"]; ok {
		if !deepEqualJSON(instance, constVal) {
			errs = append(errs, fmt.Sprintf("%s: expected const %v, got %v", path, constVal, instance))
		}
	}

	if enumVal, ok := schema["enum"].([]any); ok {
		matched := false
		for _, candidate := range enumVal {
			if deepEqualJSON(instance, candidate) {
				matched = true
				break
			}
		}
		if !matched {
			errs = append(errs, fmt.Sprintf("%s: value %v is not one of %v", path, instance, enumVal))
		}
	}

	schemaType, hasType := schema["type"].(string)
	if hasType {
		if !typeMatches(instance, schemaType) {
			errs = append(errs, fmt.Sprintf("%s: expected type '%s', got %T", path, schemaType, instance))
			return errs
		}
	}

	switch schemaType {
	case "string":
		str, _ := instance.(string)
		if pattern, ok := schema["pattern"].(string); ok {
			if matched, _ := regexp.MatchString(pattern, str); !matched {
				errs = append(errs, fmt.Sprintf("%s: value %q does not match pattern %q", path, str, pattern))
			}
		}
		if minLength, ok := schema["minLength"].(float64); ok && float64(len(str)) < minLength {
			errs = append(errs, fmt.Sprintf("%s: value %q shorter than minLength %v", path, str, minLength))
		}
	case "array":
		arr, _ := instance.([]any)
		if minItems, ok := schema["minItems"].(float64); ok && float64(len(arr)) < minItems {
			errs = append(errs, fmt.Sprintf("%s: array has %d items, fewer than minItems %v", path, len(arr), minItems))
		}
		if itemSchema, ok := schema["items"].(map[string]any); ok {
			for i, item := range arr {
				errs = append(errs, validateAt(item, itemSchema, schemaDir, fmt.Sprintf("%s[%d]", path, i))...)
			}
		}
	case "object":
		obj, _ := instance.(map[string]any)
		if required, ok := schema["required"].([]any); ok {
			for _, key := range required {
				keyStr, _ := key.(string)
				if _, present := obj[keyStr]; !present {
					errs = append(errs, fmt.Sprintf("%s: missing required field '%s'", path, keyStr))
				}
			}
		}
		properties, _ := schema["properties"].(map[string]any)
		additionalDenied := schema["additionalProperties"] == false
		for key, value := range obj {
			if propSchema, ok := properties[key].(map[string]any); ok {
				errs = append(errs, validateAt(value, propSchema, schemaDir, path+"."+key)...)
			} else if additionalDenied {
				errs = append(errs, fmt.Sprintf("%s: unknown field '%s' is not permitted by this schema", path, key))
			}
		}
	}

	return errs
}

func resolveSchema(schema map[string]any, schemaDir string) (map[string]any, error) {
	var seen map[string]bool
	for {
		value, hasRef := schema["$ref"]
		if !hasRef {
			return schema, nil
		}
		ref, ok := value.(string)
		if !ok || ref == "" || ref == "." || ref == ".." || strings.ContainsAny(ref, "/\\:#") {
			return nil, fmt.Errorf("$ref must name a schema file in the same directory: %v", value)
		}
		if seen[ref] {
			return nil, fmt.Errorf("cyclic schema reference %q", ref)
		}
		if seen == nil {
			seen = make(map[string]bool)
		}
		seen[ref] = true
		var err error
		schema, err = LoadSchema(filepath.Join(schemaDir, ref))
		if err != nil {
			return nil, fmt.Errorf("unresolved $ref %q: %w", ref, err)
		}
	}
}

func typeMatches(instance any, schemaType string) bool {
	switch schemaType {
	case "object":
		_, ok := instance.(map[string]any)
		return ok
	case "array":
		_, ok := instance.([]any)
		return ok
	case "string":
		_, ok := instance.(string)
		return ok
	case "boolean":
		_, ok := instance.(bool)
		return ok
	case "integer":
		number, ok := instance.(float64)
		return ok && math.Trunc(number) == number
	case "number":
		_, ok := instance.(float64) // encoding/json decodes all JSON numbers as float64
		return ok
	case "null":
		return instance == nil
	default:
		return false
	}
}

func deepEqualJSON(a, b any) bool {
	aBytes, aErr := json.Marshal(a)
	bBytes, bErr := json.Marshal(b)
	if aErr != nil || bErr != nil {
		return false
	}
	return string(aBytes) == string(bBytes)
}
