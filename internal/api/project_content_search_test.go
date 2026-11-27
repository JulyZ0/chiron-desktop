package api

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestSearchProjectContentMatchesLinesAndSkipsNoise(t *testing.T) {
	root := t.TempDir()
	write := func(rel, content string) {
		t.Helper()
		full := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(full, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write("src/a.ts", "const needle = 1\nunrelated\n")
	write("docs/readme.md", "NEEDLE appears here\n")
	write("node_modules/pkg/index.js", "needle should be skipped\n")
	write("binary.bin", "needle\x00binary\n")

	hits, truncated, err := searchProjectContent(context.Background(), root, "needle", 50)
	if err != nil {
		t.Fatalf("search: %v", err)
	}
	if truncated {
		t.Fatal("小目录不该截断")
	}
	if len(hits) != 2 {
		t.Fatalf("命中数=%d，want 2: %+v", len(hits), hits)
	}
	paths := map[string]bool{}
	for _, h := range hits {
		paths[h.Path] = true
		if h.Line <= 0 || !strings.Contains(strings.ToLower(h.Text), "needle") {
			t.Fatalf("命中行不对: %+v", h)
		}
	}
	if !paths["src/a.ts"] || !paths["docs/readme.md"] {
		t.Fatalf("路径不对: %+v", paths)
	}
}

func TestSearchProjectContentRespectsLimit(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "a.txt"),
		[]byte("hit\nhit\nhit\nhit\nhit\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	hits, truncated, err := searchProjectContent(context.Background(), root, "hit", 2)
	if err != nil {
		t.Fatal(err)
	}
	if len(hits) != 2 || !truncated {
		t.Fatalf("hits=%d truncated=%v，want 2/true", len(hits), truncated)
	}
}

func TestSearchProjectContentEmptyQuery(t *testing.T) {
	hits, truncated, err := searchProjectContent(context.Background(), t.TempDir(), "  ", 10)
	if err != nil || len(hits) != 0 || truncated {
		t.Fatalf("空查询应返回空结果: %+v %v %v", hits, truncated, err)
	}
}
