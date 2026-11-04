package main

import (
	"fmt"

	"github.com/spf13/cobra"
)

var instanceCmd = &cobra.Command{
	Use:   "instance",
	Short: "Manage instances",
	Long:  `Manage Chiron local service instances (.pids/state.json).`,
}

var instanceListCmd = &cobra.Command{
	Use:   "list",
	Short: "List instances",
	RunE:  runInstanceList,
}

var instanceAddCmd = &cobra.Command{
	Use:   "add <name>",
	Short: "Add a new instance",
	Args:  cobra.ExactArgs(1),
	RunE:  runInstanceAdd,
}

var instanceRemoveCmd = &cobra.Command{
	Use:   "remove <name>",
	Short: "Remove an instance",
	Args:  cobra.ExactArgs(1),
	RunE:  runInstanceRemove,
}

var (
	instancePID  int
	instancePort int
	instanceMode string
)

func init() {
	instanceAddCmd.Flags().IntVar(&instancePID, "pid", 0, "Process PID")
	instanceAddCmd.Flags().IntVar(&instancePort, "port", 0, "Listen port")
	instanceAddCmd.Flags().StringVar(&instanceMode, "mode", "manual", "Instance mode")

	instanceCmd.AddCommand(instanceListCmd)
	instanceCmd.AddCommand(instanceAddCmd)
	instanceCmd.AddCommand(instanceRemoveCmd)
}

func runInstanceList(cmd *cobra.Command, args []string) error {
	state, err := loadState()
	if err != nil {
		return err
	}
	if len(state.Instances) == 0 {
		fmt.Println("No instances recorded")
		return nil
	}

	fmt.Printf("%-16s %-8s %-8s %-16s %s\n", "NAME", "STATUS", "PID", "PORT", "MODE")
	fmt.Printf("%-16s %-8s %-8s %-16s %s\n", "----", "------", "---", "----", "----")
	for _, inst := range state.Instances {
		status := "stopped"
		if processAlive(inst.PID) {
			status = "running"
		}
		port := "-"
		if inst.Port > 0 {
			port = fmt.Sprintf("%d", inst.Port)
		}
		fmt.Printf("%-16s %-8s %-8d %-16s %s\n", inst.Name, status, inst.PID, port, inst.Mode)
	}
	return nil
}

func runInstanceAdd(cmd *cobra.Command, args []string) error {
	name := args[0]
	if instancePID <= 0 {
		return fmt.Errorf("--pid must > 0")
	}
	state, err := loadState()
	if err != nil {
		return err
	}
	state.UpsertInstance(newInstance(name, instancePID, instancePort, instanceMode, ""))
	if err := saveState(state); err != nil {
		return err
	}
	fmt.Printf("Added instance: %s (PID %d)\n", name, instancePID)
	return nil
}

func runInstanceRemove(cmd *cobra.Command, args []string) error {
	name := args[0]
	state, err := loadState()
	if err != nil {
		return err
	}
	inst := state.FindInstance(name)
	if inst == nil {
		return fmt.Errorf("instance not found: %s", name)
	}

	if inst.PID > 0 && processAlive(inst.PID) {
		if err := stopProcess(inst.PID, name); err != nil {
			return fmt.Errorf("failed to stop %s: %w", name, err)
		}
		fmt.Printf("Stopped %s (PID %d)\n", name, inst.PID)
	}

	state.RemoveInstance(name)
	if err := saveState(state); err != nil {
		return err
	}
	fmt.Printf("Removed instance: %s\n", name)
	return nil
}
