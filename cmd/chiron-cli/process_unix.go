//go:build !windows

package main

import (
	"syscall"
)

const (
	SIGTERM = syscall.SIGTERM
	SIGKILL = syscall.SIGKILL
)

func processAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	err := syscall.Kill(pid, 0)
	return err == nil || err == syscall.EPERM
}

func sendSignal(pid int, sig syscall.Signal) error {
	return syscall.Kill(pid, sig)
}
