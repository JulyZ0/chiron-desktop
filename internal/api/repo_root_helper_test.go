package api

import (
	"os"
	"path/filepath"
	"testing"
)

// readFileFromRepoRoot 读仓库内文件：本包测试运行在 internal/api 下，
// 向上找三层到仓库根（internal/api → internal → root）。
func readFileFromRepoRoot(rel string) (string, error) {
	root, err := filepath.Abs(filepath.Join("..", ".."))
	if err != nil {
		return "", err
	}
	raw, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(rel)))
	if err != nil {
		return "", err
	}
	return string(raw), nil
}

// 防呆：确保 helper 找到的确实是仓库根（否则上面的源码守卫会静默失效）。
func TestRepoRootHelperPointsAtRepo(t *testing.T) {
	raw, err := readFileFromRepoRoot("go.mod")
	if err != nil {
		t.Fatalf("读不到 go.mod，helper 路径不对: %v", err)
	}
	if len(raw) == 0 {
		t.Fatal("go.mod 为空")
	}
}
