//go:build unix

package api

// project_files_unix_test.go —— 非常规文件（命名管道）必须被拦在 open 之前。
//
// 这个用例只能在 Unix 上跑：Windows 没有 FIFO，`os.Stat("NUL")` 虽然也返回
// device 模式（同样被判为非 regular），但那不是这里要证明的东西 —— 要证明的是
// "打开管道会挂死，而我们的判据在打开之前就返回了"。
//
// 每个用例都把被测调用放进 goroutine 并设超时。这不是防御性写法，而是必需：
// 若判据回归（被挪到 open 之后），open(2) 会永久阻塞，直接写在主 goroutine 里
// 会让 `go test` 整个挂住，CI 上表现为超时而不是一条失败用例。

import (
	"errors"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

// fifoProbeTimeout 是"判定为挂死"的等待上限。真实的 open 正常在毫秒级返回，
// 5 秒只是给慢文件系统留余地。
const fifoProbeTimeout = 5 * time.Second

func makeFIFO(t *testing.T, root, name string) {
	t.Helper()
	if err := syscall.Mkfifo(filepath.Join(root, name), 0o644); err != nil {
		t.Skipf("当前文件系统不支持 mkfifo: %v", err)
	}
}

// 文本预览端点：FIFO 必须被拒为 errNotRegularFile，而不是挂住。
func TestOpenProjectFileRejectsFIFO(t *testing.T) {
	root := t.TempDir()
	makeFIFO(t, root, "pipe")

	type result struct {
		err error
	}
	done := make(chan result, 1)
	go func() {
		_, _, err := openProjectFile(root, "pipe", maxProjectFileBytes, errFileTooLarge)
		done <- result{err: err}
	}()

	select {
	case got := <-done:
		if !errors.Is(got.err, errNotRegularFile) {
			t.Fatalf("期望 errNotRegularFile，得到 %v", got.err)
		}
	case <-time.After(fifoProbeTimeout):
		t.Fatal("openProjectFile 在 FIFO 上阻塞了 —— 这正是要防的挂死")
	}
}

// `@` 引用路径：返回说明文本而不是错误（文件确实存在，只是内容不是可内联的
// 文本 —— 这属于模型该知道的事实，不该让整条消息失败）。
func TestReadRefContentRejectsFIFO(t *testing.T) {
	root := t.TempDir()
	makeFIFO(t, root, "pipe")

	done := make(chan refContent, 1)
	go func() {
		c, _ := readRefContent(root, "pipe")
		done <- c
	}()

	select {
	case got := <-done:
		if got.text == "" {
			t.Fatal("应返回说明文本，而不是空内容")
		}
		if got.isDir {
			t.Fatal("FIFO 不是目录")
		}
	case <-time.After(fifoProbeTimeout):
		t.Fatal("readRefContent 在 FIFO 上阻塞了")
	}
}
