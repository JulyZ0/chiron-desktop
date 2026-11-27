package api

// 项目内容搜索（F6，只读）。
//
// 与 `searchProjectFiles` 的区别：那个按**文件名/路径**匹配，给 `@` 补全用；这里读文件
// 内容并按行返回命中。约束全部是"防一次搜索拖死网关"：
// - 跳过依赖/构建/元数据目录；
// - 单文件 ≤1MB、总读取 ≤16MB、最多扫 3000 个文件；
// - 命中上限 + 每行截断；返回 truncated 让界面如实说明。
// 客户端的项目（文件在用户设备上）暂不支持：逐个 RPC 读文件会把"搜索"变成几百次往返。

import (
	"context"
	"io"
	"os"
	"path/filepath"
	"strings"
)

type ProjectContentHit struct {
	Path string `json:"path"`
	Line int    `json:"line"`
	Text string `json:"text"`
}

const (
	contentSearchMaxFiles = 3000
	contentSearchMaxBytes = 16 << 20
	contentSearchFileMax  = 1 << 20
	contentSearchLineMax  = 240
	contentSearchHitsMax  = 200
)

var contentSearchSkipDirs = map[string]bool{
	".git": true, ".svn": true, ".hg": true, "node_modules": true,
	"dist": true, "build": true, "out": true, "target": true,
	".venv": true, "venv": true, "__pycache__": true, ".mypy_cache": true,
	".pytest_cache": true, ".next": true, ".nuxt": true, "vendor": true,
	"coverage": true, ".idea": true, ".vscode": true,
}

// searchProjectContent 在 root 内按行搜索 query（大小写不敏感）。
func searchProjectContent(
	ctx context.Context, root, query string, limit int,
) ([]ProjectContentHit, bool, error) {
	q := strings.ToLower(strings.TrimSpace(query))
	hits := make([]ProjectContentHit, 0)
	if q == "" {
		return hits, false, nil
	}
	if limit <= 0 || limit > contentSearchHitsMax {
		limit = 50
	}
	realRoot, err := filepath.EvalSymlinks(root)
	if err != nil {
		return nil, false, errRootUnavailable
	}

	files := 0
	readBytes := 0
	truncated := false
	err = filepath.WalkDir(realRoot, func(path string, d os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return nil // 单个目录不可读不该让整次搜索失败
		}
		select {
		case <-ctx.Done():
			truncated = true
			return ctx.Err()
		default:
		}
		name := d.Name()
		if d.IsDir() {
			if path != realRoot && contentSearchSkipDirs[name] {
				return filepath.SkipDir
			}
			return nil
		}
		if !d.Type().IsRegular() {
			return nil
		}
		files++
		if files > contentSearchMaxFiles || readBytes >= contentSearchMaxBytes {
			truncated = true
			return filepath.SkipAll
		}
		rel, err := filepath.Rel(realRoot, path)
		if err != nil {
			return nil
		}
		rel = filepath.ToSlash(rel)
		f, err := os.Open(path)
		if err != nil {
			return nil
		}
		buf, err := io.ReadAll(io.LimitReader(f, contentSearchFileMax))
		_ = f.Close() // 不能 defer：Walk 里会累积到几千个未关闭句柄
		if err != nil {
			return nil
		}
		readBytes += len(buf)
		if isBinaryContent(buf) {
			return nil
		}
		lines := strings.Split(string(buf), "\n")
		for i, line := range lines {
			line = strings.TrimRight(line, "\r")
			if !strings.Contains(strings.ToLower(line), q) {
				continue
			}
			text := strings.TrimSpace(line)
			if len(text) > contentSearchLineMax {
				text = text[:contentSearchLineMax] + "…"
			}
			hits = append(hits, ProjectContentHit{Path: rel, Line: i + 1, Text: text})
			if len(hits) >= limit {
				truncated = true
				return filepath.SkipAll
			}
		}
		return nil
	})
	if err != nil && err != context.Canceled && err != context.DeadlineExceeded {
		return hits, truncated, err
	}
	return hits, truncated, nil
}

// isBinaryContent 用 NUL 字节判断二进制；只检查前 512 字节（与 git 同口径）。
func isBinaryContent(b []byte) bool {
	n := len(b)
	if n > 512 {
		n = 512
	}
	for i := 0; i < n; i++ {
		if b[i] == 0 {
			return true
		}
	}
	return false
}
