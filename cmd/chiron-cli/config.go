package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/spf13/cobra"
	"gopkg.in/yaml.v3"
)

var configCmd = &cobra.Command{
	Use:   "config",
	Short: "Manage configuration",
	Long:  `Manage Chiron configuration.`,
}

var configShowCmd = &cobra.Command{
	Use:   "show",
	Short: "Show current configuration",
	RunE:  runConfigShow,
}

var configGetCmd = &cobra.Command{
	Use:   "get <key>",
	Short: "Get a configuration value",
	Args:  cobra.ExactArgs(1),
	RunE:  runConfigGet,
}

var configSetCmd = &cobra.Command{
	Use:   "set <key> <value>",
	Short: "Set a configuration value",
	Args:  cobra.ExactArgs(2),
	RunE:  runConfigSet,
}

var configExportCmd = &cobra.Command{
	Use:   "export",
	Short: "Export configuration",
	RunE:  runConfigExport,
}

var configImportCmd = &cobra.Command{
	Use:   "import <file>",
	Short: "Import configuration",
	Args:  cobra.ExactArgs(1),
	RunE:  runConfigImport,
}

var (
	configFormat string
)

func init() {
	configCmd.AddCommand(configShowCmd)
	configCmd.AddCommand(configGetCmd)
	configCmd.AddCommand(configSetCmd)
	configCmd.AddCommand(configExportCmd)
	configCmd.AddCommand(configImportCmd)

	configExportCmd.Flags().StringVarP(&configFormat, "format", "f", "json", "Export format: json or yaml")
}

func runConfigShow(cmd *cobra.Command, args []string) error {
	// Load config from environment/file
	cfg := loadConfig()

	// Print as JSON
	data, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return fmt.Errorf("failed to marshal config: %w", err)
	}

	fmt.Println(string(data))
	return nil
}

func runConfigGet(cmd *cobra.Command, args []string) error {
	key := args[0]

	// Get value from environment
	value := os.Getenv(key)
	if value == "" {
		return fmt.Errorf("configuration key not found: %s", key)
	}

	fmt.Printf("%s=%s\n", key, value)
	return nil
}

func runConfigSet(cmd *cobra.Command, args []string) error {
	key := args[0]
	value := args[1]

	// Set environment variable
	if err := os.Setenv(key, value); err != nil {
		return fmt.Errorf("failed to set configuration: %w", err)
	}

	fmt.Printf("Set %s=%s\n", key, value)
	return nil
}

func runConfigExport(cmd *cobra.Command, args []string) error {
	cfg := loadConfig()

	var data []byte
	var err error

	switch configFormat {
	case "json":
		data, err = json.MarshalIndent(cfg, "", "  ")
	case "yaml":
		data, err = yaml.Marshal(cfg)
	default:
		return fmt.Errorf("unsupported format: %s", configFormat)
	}

	if err != nil {
		return fmt.Errorf("failed to marshal config: %w", err)
	}

	fmt.Println(string(data))
	return nil
}

func runConfigImport(cmd *cobra.Command, args []string) error {
	file := args[0]

	data, err := os.ReadFile(file)
	if err != nil {
		return fmt.Errorf("failed to read file: %w", err)
	}

	var cfg map[string]interface{}

	// Try JSON first
	if err := json.Unmarshal(data, &cfg); err != nil {
		// Try YAML
		if err := yaml.Unmarshal(data, &cfg); err != nil {
			return fmt.Errorf("failed to parse config file: %w", err)
		}
	}

	// Set environment variables
	for key, value := range cfg {
		if strValue, ok := value.(string); ok {
			os.Setenv(key, strValue)
		}
	}

	fmt.Printf("Imported configuration from %s\n", file)
	return nil
}

func loadConfig() map[string]interface{} {
	return map[string]interface{}{
		"PORT":            os.Getenv("PORT"),
		"POSTGRES_DSN":    os.Getenv("POSTGRES_DSN"),
		"REDIS_MODE":      os.Getenv("REDIS_MODE"),
		"REDIS_ADDR":      os.Getenv("REDIS_ADDR"),
		"STORAGE_BACKEND": os.Getenv("STORAGE_BACKEND"),
		"LLM_PROVIDER":    os.Getenv("LLM_PROVIDER"),
		"LOG_LEVEL":       os.Getenv("LOG_LEVEL"),
	}
}
