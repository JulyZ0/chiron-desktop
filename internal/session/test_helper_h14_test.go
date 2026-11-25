package session

import (
	"os"
	"path/filepath"
)

// readFileRelative 读本包目录下的文件（H14 的源码守卫用）。
func readFileRelative(name string) (string, error) {
	raw, err := os.ReadFile(filepath.Clean(name))
	if err != nil {
		return "", err
	}
	return string(raw), nil
}
