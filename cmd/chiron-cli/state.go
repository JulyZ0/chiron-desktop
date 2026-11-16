package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

type InstanceState struct {
	Name      string `json:"name"`
	PID       int    `json:"pid"`
	Port      int    `json:"port"`
	StartedAt string `json:"started_at"`
	LogFile   string `json:"log_file"`
	Mode      string `json:"mode,omitempty"`
}

type State struct {
	Instances []InstanceState `json:"instances"`
}

func stateFilePath() string {
	cwd, err := os.Getwd()
	if err != nil {
		return filepath.Join(".", ".pids", "state.json")
	}
	return filepath.Join(cwd, ".pids", "state.json")
}

func loadState() (*State, error) {
	path := stateFilePath()
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return &State{Instances: []InstanceState{}}, nil
		}
		return nil, fmt.Errorf("cant load state: %w", err)
	}
	var s State
	if err := json.Unmarshal(data, &s); err != nil {
		return nil, fmt.Errorf("failed to unmarshal JSON from %s: %w", path, err)
	}
	if s.Instances == nil {
		s.Instances = []InstanceState{}
	}
	return &s, nil
}

func saveState(s *State) error {
	path := stateFilePath()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("failed to create dir %s: %w", filepath.Dir(path), err)
	}
	data, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return fmt.Errorf("failed to marshal state to JSON: %w", err)
	}
	// Write the JSON to a temporary file and rename it to the state file
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return fmt.Errorf("failed to write state to %s: %w", tmp, err)
	}
	return os.Rename(tmp, path)
}

func (s *State) UpsertInstance(inst InstanceState) {
	for i := range s.Instances {
		if s.Instances[i].Name == inst.Name {
			s.Instances[i] = inst
			return
		}
	}
	s.Instances = append(s.Instances, inst)
}

func (s *State) RemoveInstance(name string) bool {
	for i, inst := range s.Instances {
		if inst.Name == name {
			s.Instances = append(s.Instances[:i], s.Instances[i+1:]...)
			return true
		}
	}
	return false
}

func (s *State) FindInstance(name string) *InstanceState {
	for i := range s.Instances {
		if s.Instances[i].Name == name {
			return &s.Instances[i]
		}
	}
	return nil
}

func newInstance(name string, pid, port int, mode, logFile string) InstanceState {
	return InstanceState{
		Name:      name,
		PID:       pid,
		Port:      port,
		Mode:      mode,
		StartedAt: time.Now().UTC().Format(time.RFC3339),
		LogFile:   logFile,
	}
}
