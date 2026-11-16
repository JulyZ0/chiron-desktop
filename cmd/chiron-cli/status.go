package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"github.com/spf13/cobra"
)

var statusCmd = &cobra.Command{
	Use:   "status",
	Short: "Show service status",
	Long:  `Show the current status of Chiron services.`,
	RunE:  runStatus,
}

var statusAddr string

func init() {
	statusCmd.Flags().StringVarP(&statusAddr, "addr", "a", "http://localhost:8080", "Service address")
}

// ServiceStatus represents the status response from a Chiron service.
type ServiceStatus struct {
	Status   string            `json:"status"`
	Version  string            `json:"version"`
	Uptime   string            `json:"uptime"`
	Mode     string            `json:"mode"`
	Services map[string]string `json:"services"`
}

func runStatus(cmd *cobra.Command, args []string) error {
	client := &http.Client{Timeout: 5 * time.Second}

	resp, err := client.Get(fmt.Sprintf("%s/health", statusAddr))
	if err != nil {
		return fmt.Errorf("failed to connect to service: %w", err)
	}
	defer resp.Body.Close()

	var status ServiceStatus
	if err := json.NewDecoder(resp.Body).Decode(&status); err != nil {
		return fmt.Errorf("failed to decode response: %w", err)
	}

	// Print status
	fmt.Printf("chiron Service Status\n")
	fmt.Printf("=====================\n")
	fmt.Printf("Status:    %s\n", status.Status)
	fmt.Printf("Version:   %s\n", status.Version)
	fmt.Printf("Uptime:    %s\n", status.Uptime)
	fmt.Printf("Mode:      %s\n", status.Mode)
	fmt.Printf("\nServices:\n")

	for name, svcStatus := range status.Services {
		fmt.Printf("  %s: %s\n", name, svcStatus)
	}

	return nil
}
