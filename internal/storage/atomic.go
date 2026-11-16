package storage

import (
	"context"
	"io"
	"os"
	"sync/atomic"
)

// AtomicStore wraps a FileStore and supports atomic hot-swapping.
type AtomicStore struct {
	current atomic.Pointer[FileStore]
}

// NewAtomicStore creates an AtomicStore with the given initial backend.
func NewAtomicStore(initial FileStore) *AtomicStore {
	a := &AtomicStore{}
	a.current.Store(&initial)
	return a
}

func (a *AtomicStore) load() FileStore {
	return *a.current.Load()
}

func (a *AtomicStore) Read(ctx context.Context, path string) ([]byte, error) {
	return a.load().Read(ctx, path)
}

func (a *AtomicStore) Write(ctx context.Context, path string, data []byte) error {
	return a.load().Write(ctx, path, data)
}

func (a *AtomicStore) WriteStream(ctx context.Context, path string, r io.Reader, size int64, perm os.FileMode) error {
	return a.load().WriteStream(ctx, path, r, size, perm)
}

func (a *AtomicStore) OpenStream(ctx context.Context, path string) (io.ReadCloser, error) {
	return a.load().OpenStream(ctx, path)
}

func (a *AtomicStore) Delete(ctx context.Context, path string) error {
	return a.load().Delete(ctx, path)
}

func (a *AtomicStore) List(ctx context.Context, prefix string) ([]FileInfo, error) {
	return a.load().List(ctx, prefix)
}

// Swap atomically replaces the underlying FileStore backend.
func (a *AtomicStore) Swap(new FileStore) {
	a.current.Store(&new)
}

// Backend returns the type name of the current backend: "local" or "s3".
func (a *AtomicStore) Backend() string {
	switch a.load().(type) {
	case *LocalStore:
		return "local"
	case *S3Store:
		return "s3"
	default:
		return "unknown"
	}
}

// LoadRaw returns the current underlying FileStore (for inspection).
func (a *AtomicStore) LoadRaw() FileStore {
	return a.load()
}
