package db

import (
	"testing"
)

func TestRedactDSN(t *testing.T) {
	tests := []struct {
		name     string
		input    string
		expected string
	}{
		{
			name:     "standard DSN with password",
			input:    "postgres://user:secret123@localhost:5432/db",
			expected: "postgres://user:***@localhost:5432/db",
		},
		{
			name:     "DSN without password",
			input:    "postgres://user@localhost:5432/db",
			expected: "postgres://user@localhost:5432/db",
		},
		{
			name:     "DSN with empty password",
			input:    "postgres://user:@localhost:5432/db",
			expected: "postgres://user:***@localhost:5432/db",
		},
		{
			name:     "DSN with special chars in password",
			input:    "postgres://admin:p@ssw0rd!@host:5432/mydb",
			expected: "postgres://admin:***@host:5432/mydb",
		},
		{
			name:     "no at sign — not a valid DSN",
			input:    "not-a-dsn",
			expected: "not-a-dsn",
		},
		{
			name:     "empty string",
			input:    "",
			expected: "",
		},
		{
			name:     "postgresql scheme",
			input:    "postgresql://user:pass@host:5432/db",
			expected: "postgresql://user:***@host:5432/db",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := redactDSN(tt.input)
			if got != tt.expected {
				t.Errorf("redactDSN(%q) = %q, want %q", tt.input, got, tt.expected)
			}
		})
	}
}

func TestDefaultPoolConfig(t *testing.T) {
	cfg := DefaultPoolConfig()
	if cfg.MaxConns <= 0 {
		t.Errorf("MaxConns should be positive, got %d", cfg.MaxConns)
	}
	if cfg.MinConns <= 0 {
		t.Errorf("MinConns should be positive, got %d", cfg.MinConns)
	}
	if cfg.MaxConnLifetime <= 0 {
		t.Errorf("MaxConnLifetime should be positive, got %v", cfg.MaxConnLifetime)
	}
	if cfg.MaxConnIdleTime <= 0 {
		t.Errorf("MaxConnIdleTime should be positive, got %v", cfg.MaxConnIdleTime)
	}
	if cfg.HealthCheckPeriod <= 0 {
		t.Errorf("HealthCheckPeriod should be positive, got %v", cfg.HealthCheckPeriod)
	}
}