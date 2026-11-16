//go:build windows

package main

import (
	"fmt"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
)

const (
	SIGTERM = syscall.Signal(0)
	SIGKILL = syscall.Signal(0)
)

func processAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	out, err := exec.Command("tasklist", "/FI", fmt.Sprintf("PID eq %d", pid), "/NH").Output()
	if err != nil {
		return false
	}
	want := strconv.Itoa(pid)
	for _, line := range strings.Split(string(out), "\n") {
		fields := strings.Fields(line)
		if len(fields) >= 2 && fields[1] == want {
			return true
		}
	}
	return false
}

func sendSignal(pid int, sig syscall.Signal) error {
	return fmt.Errorf("signals not supported on windows")
}
