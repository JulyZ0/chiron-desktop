package id

import (
	"crypto/rand"
	"fmt"
)

// UUID returns a random (RFC 4122 version 4) UUID string.
//
// The PostgreSQL schema uses uuid-typed columns for primary/foreign keys,
// while NextID() produces compact base62 strings that are not valid UUIDs.
// Use UUID() anywhere an ID is written to a uuid column (sessions, messages, ...).
func UUID() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", fmt.Errorf("id: crypto/rand unavailable: %w", err)
	}
	// RFC 4122: set version (4) and variant (10xx).
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16]), nil
}
