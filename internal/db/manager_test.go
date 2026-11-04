package db

import (
	"context"
	"errors"
	"testing"
	"time"
)

// errorRow implements pgx.Row for error-path testing.
type testErrorRow struct {
	err error
}

func (r testErrorRow) Scan(dest ...interface{}) error {
	return r.err
}

func TestNewDBManager(t *testing.T) {
	m := NewDBManager()
	if m == nil {
		t.Fatal("NewDBManager() returned nil")
	}
}

func TestDBManager_PoolNotAvailable(t *testing.T) {
	m := NewDBManager()
	// Pool is nil by default
	_, err := m.GetPool()
	if !errors.Is(err, ErrDatabaseNotAvailable) {
		t.Errorf("expected ErrDatabaseNotAvailable, got %v", err)
	}

	_, err = m.GetReadPool()
	if !errors.Is(err, ErrDatabaseNotAvailable) {
		t.Errorf("expected ErrDatabaseNotAvailable, got %v", err)
	}

	if m.IsAvailable() {
		t.Error("IsAvailable() should be false when Pool is nil")
	}
}

func TestDBManager_QueryRow_Error(t *testing.T) {
	m := NewDBManager()
	row := m.QueryRow(context.Background(), "SELECT 1")
	var v int
	err := row.Scan(&v)
	if err == nil {
		t.Fatal("expected error, got nil")
	}
}

func TestDBManager_Ping_Error(t *testing.T) {
	m := NewDBManager()
	err := m.Ping(context.Background())
	if !errors.Is(err, ErrDatabaseNotAvailable) {
		t.Errorf("expected ErrDatabaseNotAvailable, got %v", err)
	}
}

func TestDBManager_Exec_Error(t *testing.T) {
	m := NewDBManager()
	_, err := m.Exec(context.Background(), "SELECT 1")
	if !errors.Is(err, ErrDatabaseNotAvailable) {
		t.Errorf("expected ErrDatabaseNotAvailable, got %v", err)
	}
}

func TestDBManager_Begin_Error(t *testing.T) {
	m := NewDBManager()
	_, err := m.Begin(context.Background())
	if !errors.Is(err, ErrDatabaseNotAvailable) {
		t.Errorf("expected ErrDatabaseNotAvailable, got %v", err)
	}
}

func TestDBManager_FetchOne_Error(t *testing.T) {
	m := NewDBManager()
	_, err := m.FetchOne(context.Background(), "SELECT 1")
	if !errors.Is(err, ErrDatabaseNotAvailable) {
		t.Errorf("expected ErrDatabaseNotAvailable, got %v", err)
	}
}

func TestDBManager_FetchAll_Error(t *testing.T) {
	m := NewDBManager()
	_, err := m.FetchAll(context.Background(), "SELECT 1")
	if !errors.Is(err, ErrDatabaseNotAvailable) {
		t.Errorf("expected ErrDatabaseNotAvailable, got %v", err)
	}
}

func TestDBManager_AutoTuneDefaults(t *testing.T) {
	m := NewDBManager()
	m.mu.Lock()
	interval := m.tuneInterval
	minConns := m.minConns
	maxConns := m.maxConns
	targetUtil := m.targetUtilization
	autoTunerRunning := m.autoTunerRunning
	m.mu.Unlock()

	if interval != 0 {
		t.Errorf("expected zero tuneInterval, got %v", interval)
	}
	if minConns != 0 {
		t.Errorf("expected zero minConns, got %d", minConns)
	}
	if maxConns != 0 {
		t.Errorf("expected zero maxConns, got %d", maxConns)
	}
	if targetUtil != 0 {
		t.Errorf("expected zero targetUtilization, got %f", targetUtil)
	}
	if autoTunerRunning {
		t.Error("autoTunerRunning should be false by default")
	}
}

func TestPoolConfigDefaults(t *testing.T) {
	cfg := DefaultPoolConfig()
	if cfg.MaxConns != 20 {
		t.Errorf("expected MaxConns=20, got %d", cfg.MaxConns)
	}
	if cfg.MinConns != 2 {
		t.Errorf("expected MinConns=2, got %d", cfg.MinConns)
	}
	if cfg.MaxConnLifetime != 30*time.Minute {
		t.Errorf("expected MaxConnLifetime=30m, got %v", cfg.MaxConnLifetime)
	}
	if cfg.MaxConnIdleTime != 5*time.Minute {
		t.Errorf("expected MaxConnIdleTime=5m, got %v", cfg.MaxConnIdleTime)
	}
	if cfg.HealthCheckPeriod != 30*time.Second {
		t.Errorf("expected HealthCheckPeriod=30s, got %v", cfg.HealthCheckPeriod)
	}
}