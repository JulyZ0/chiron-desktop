package main

import (
	"bufio"
	"bytes"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/spf13/cobra"
)

var logsCmd = &cobra.Command{
	Use:   "logs",
	Short: "View logs",
	Long:  `View Chiron service logs (logs/{service}.stdout.log / .stderr.log).`,
	RunE:  runLogs,
}

var (
	logsService string
	logsTail    int
	logsFollow  bool
)

var serviceNameRe = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)

func init() {
	logsCmd.Flags().StringVarP(&logsService, "service", "s", "", "Service name (e.g. gateway, python-engine)")
	logsCmd.Flags().IntVarP(&logsTail, "tail", "t", 100, "Number of lines to show")
	logsCmd.Flags().BoolVarP(&logsFollow, "follow", "f", false, "Follow log output")
}

func runLogs(cmd *cobra.Command, args []string) error {
	if logsService == "" {
		entries, err := os.ReadDir("logs")
		if err != nil {
			return fmt.Errorf("? %w", err)
		}
		fmt.Println("Available log files in logs/:")
		for _, e := range entries {
			if !e.IsDir() {
				fmt.Printf("  %s\n", e.Name())
			}
		}
		return nil
	}

	if !serviceNameRe.MatchString(logsService) {
		return fmt.Errorf("invalid service name: %s, please use -s or --service to specify a service", logsService)
	}

	logPaths := []string{
		filepath.Join("logs", logsService+".stdout.log"),
		filepath.Join("logs", logsService+".stderr.log"),
	}

	// 至少一个日志文件存在
	existing := false
	for _, p := range logPaths {
		if _, err := os.Stat(p); err == nil {
			existing = true
			break
		}
	}
	if !existing {
		return fmt.Errorf("log file not found: %s.*.log in logs/, please use -s or --service to specify a service", logsService)
	}

	for _, p := range logPaths {
		if _, err := os.Stat(p); err != nil {
			continue
		}
		if err := tailFile(p, logsTail); err != nil {
			return err
		}
	}

	if logsFollow {
		return followLogs(logPaths)
	}
	return nil
}

func tailFile(path string, n int) error {
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()

	fmt.Printf("===== %s =====\n", path)
	lines, err := readLastLines(file, n)
	if err != nil {
		return fmt.Errorf("璇诲彇 %s 澶辫触: %w", path, err)
	}
	for _, line := range lines {
		fmt.Println(line)
	}
	return nil
}

func readLastLines(file *os.File, n int) ([]string, error) {
	if n <= 0 {
		n = 100
	}
	info, err := file.Stat()
	if err != nil {
		return nil, err
	}
	size := info.Size()
	if size == 0 {
		return nil, nil
	}

	const maxBytes = 1 << 20
	var data []byte
	pos := size
	newlines := 0
	atHead := false
	for pos > 0 && len(data) < maxBytes && newlines <= n {
		readSize := int64(4096)
		if pos < readSize {
			readSize = pos
			atHead = true
		}
		pos -= readSize
		chunk := make([]byte, readSize)
		if _, err := file.ReadAt(chunk, pos); err != nil && err != io.EOF {
			return nil, err
		}
		data = append(chunk, data...)
		newlines = bytes.Count(data, []byte{'\n'})
	}

	content := string(data)
	if !atHead {
		if idx := strings.Index(content, "\n"); idx >= 0 {
			content = content[idx+1:]
		} else {
			content = ""
		}
	}
	lines := strings.Split(content, "\n")
	if len(lines) > 0 && lines[len(lines)-1] == "" {
		lines = lines[:len(lines)-1]
	}
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return lines, nil
}

func followLogs(paths []string) error {
	// 璁板綍鍚勬枃浠跺綋鍓嶄綅缃紙鏂囦欢灏撅級
	offsets := map[string]int64{}
	for _, p := range paths {
		file, err := os.Open(p)
		if err != nil {
			continue
		}
		off, _ := file.Seek(0, 2)
		offsets[p] = off
		file.Close()
	}

	fmt.Println("Following logs... (Ctrl+C to stop)")
	for {
		for _, p := range paths {
			file, err := os.Open(p)
			if err != nil {
				continue
			}
			off := offsets[p]
			if _, err := file.Seek(off, 0); err == nil {
				reader := bufio.NewReader(file)
				for {
					line, err := reader.ReadString('\n')
					if line != "" {
						fmt.Print(line)
					}
					if err != nil {
						break // EOF 或错误
					}
				}
				newOff, _ := file.Seek(0, 1)
				offsets[p] = newOff
			}
			file.Close()
		}
		time.Sleep(500 * time.Millisecond)
	}
}
