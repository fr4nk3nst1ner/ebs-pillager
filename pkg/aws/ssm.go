package aws

import (
	"context"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
	"github.com/aws/aws-sdk-go-v2/service/ssm/types"
)

// SSMOperations handles SSM operations
type SSMOperations struct {
	client *Client
}

// NewSSMOperations creates a new SSM operations handler
func NewSSMOperations(client *Client) *SSMOperations {
	return &SSMOperations{
		client: client,
	}
}

// RunCommand executes a command on an instance using SSM
func (s *SSMOperations) RunCommand(ctx context.Context, instanceID string, command string, timeoutSeconds int32) error {
	log.Printf("Executing SSM command on instance %s with timeout %d seconds", instanceID, timeoutSeconds)
	log.Printf("Command to execute:\n%s", command)

	input := &ssm.SendCommandInput{
		DocumentName:   aws.String("AWS-RunShellScript"),
		InstanceIds:   []string{instanceID},
		TimeoutSeconds: aws.Int32(timeoutSeconds),
		Parameters: map[string][]string{
			"commands": {command},
		},
	}

	output, err := s.client.SSM.SendCommand(ctx, input)
	if err != nil {
		return fmt.Errorf("failed to send SSM command: %w", err)
	}

	commandID := *output.Command.CommandId
	log.Printf("SSM command sent successfully. Command ID: %s", commandID)

	// Stream command output while waiting for completion
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
			// Get command invocation
			invocation, err := s.client.SSM.GetCommandInvocation(ctx, &ssm.GetCommandInvocationInput{
				CommandId:  aws.String(commandID),
				InstanceId: aws.String(instanceID),
			})
			if err != nil {
				if strings.Contains(err.Error(), "InvocationDoesNotExist") {
					log.Printf("Command invocation not yet available, waiting...")
					continue
				}
				return fmt.Errorf("failed to get command invocation: %w", err)
			}

			// Print any new output
			if invocation.StandardOutputContent != nil && *invocation.StandardOutputContent != "" {
				log.Printf("Command output:\n%s", *invocation.StandardOutputContent)
			}
			if invocation.StandardErrorContent != nil && *invocation.StandardErrorContent != "" {
				log.Printf("Command error output:\n%s", *invocation.StandardErrorContent)
			}

			// Check command status
			status := invocation.Status
			log.Printf("Command status: %s", status)

			switch status {
			case types.CommandInvocationStatusSuccess:
				return nil
			case types.CommandInvocationStatusFailed, types.CommandInvocationStatusCancelled, types.CommandInvocationStatusTimedOut:
				return fmt.Errorf("command failed with status %s: %s", status, aws.ToString(invocation.StandardErrorContent))
			}
		}
	}
}

// RunTrufflehog runs Trufflehog on the specified instance
func (s *SSMOperations) RunTrufflehog(ctx context.Context, instanceID, mountPath, pillagePath string, jsonOutput bool) error {
	// Install required packages
	setupCmd := `
		# Update package list and install required packages
		apt-get update && apt-get install -y python3-pip || \
		yum update -y && yum install -y python3-pip
		
		# Install Trufflehog
		pip3 install trufflehog
	`
	if err := s.RunCommand(ctx, instanceID, setupCmd, 600); err != nil {
		return fmt.Errorf("failed to install required packages: %w", err)
	}

	// Create mount directory
	if err := s.RunCommand(ctx, instanceID, fmt.Sprintf("mkdir -p %s", mountPath), 60); err != nil {
		return fmt.Errorf("failed to create mount directory: %w", err)
	}

	// Run Trufflehog
	searchPath := filepath.Join(mountPath, pillagePath)
	trufflehogCmd := fmt.Sprintf("trufflehog filesystem %s", searchPath)
	if jsonOutput {
		trufflehogCmd += " --json"
	}

	if err := s.RunCommand(ctx, instanceID, trufflehogCmd, 3600); err != nil {
		return fmt.Errorf("failed to run Trufflehog: %w", err)
	}

	return nil
}

// SaveCommandOutput saves the output of the last command to a file
func (s *SSMOperations) SaveCommandOutput(ctx context.Context, instanceID, outFile string) error {
	// Get the latest command invocation
	input := &ssm.ListCommandInvocationsInput{
		InstanceId: aws.String(instanceID),
		MaxResults: aws.Int32(1),
	}

	result, err := s.client.SSM.ListCommandInvocations(ctx, input)
	if err != nil {
		return fmt.Errorf("failed to list command invocations: %w", err)
	}

	if len(result.CommandInvocations) == 0 {
		return fmt.Errorf("no command invocations found for instance %s", instanceID)
	}

	// Get the full command output
	commandID := *result.CommandInvocations[0].CommandId
	invocation, err := s.client.SSM.GetCommandInvocation(ctx, &ssm.GetCommandInvocationInput{
		CommandId:  aws.String(commandID),
		InstanceId: aws.String(instanceID),
	})
	if err != nil {
		return fmt.Errorf("failed to get command output: %w", err)
	}

	// Write output to file
	output := aws.ToString(invocation.StandardOutputContent)
	if err := os.WriteFile(outFile, []byte(output), 0644); err != nil {
		return fmt.Errorf("failed to write output to file: %w", err)
	}

	log.Printf("Command output saved to %s", outFile)
	return nil
} 