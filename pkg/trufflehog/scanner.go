package trufflehog

import (
	"context"
	"fmt"
	"path/filepath"

	"ebs-pillage/pkg/aws"
)

// Scanner represents a Trufflehog scanner
type Scanner struct {
	ssmOps *aws.SSMOperations
}

// NewScanner creates a new Trufflehog scanner
func NewScanner(ssmOps *aws.SSMOperations) *Scanner {
	return &Scanner{
		ssmOps: ssmOps,
	}
}

// ScanVolume runs Trufflehog on a mounted volume
func (s *Scanner) ScanVolume(ctx context.Context, instanceID, mountPath, pillagePath, deviceName string, jsonOutput bool) error {
	// First mount the volume
	if err := s.mountVolume(ctx, instanceID, mountPath, deviceName); err != nil {
		return fmt.Errorf("failed to mount volume: %w", err)
	}

	// Then run the scan
	if err := s.runScan(ctx, instanceID, mountPath, pillagePath, jsonOutput); err != nil {
		// Try to unmount even if scan fails
		unmountErr := s.unmountVolume(ctx, instanceID, mountPath)
		if unmountErr != nil {
			return fmt.Errorf("scan failed: %v, unmount failed: %v", err, unmountErr)
		}
		return fmt.Errorf("scan failed: %w", err)
	}

	// Finally unmount
	if err := s.unmountVolume(ctx, instanceID, mountPath); err != nil {
		return fmt.Errorf("failed to unmount volume: %w", err)
	}

	return nil
}

// mountVolume mounts the volume at the specified path
func (s *Scanner) mountVolume(ctx context.Context, instanceID, mountPath, deviceName string) error {
	// Command to mount the volume with improved device detection and error handling
	mountCmd := fmt.Sprintf(`
		set -x  # Enable command tracing
		set -e  # Exit on error

		# Install required packages
		if command -v apt-get > /dev/null 2>&1; then
			sudo apt-get update
			sudo apt-get install -y nvme-cli jq
		elif command -v yum > /dev/null 2>&1; then
			sudo yum install -y nvme-cli jq
		else
			echo "No supported package manager found"
			exit 1
		fi

		# Check if mount path is currently mounted
		echo "Checking if %s is currently mounted..."
		if mount | grep -i %s; then
			echo "%s is currently mounted. Unmounting..."
			sudo umount %s
			if [ $? -ne 0 ]; then
				echo "Failed to unmount %s"
				exit 1
			fi
			echo "Unmounted %s successfully."
		fi

		# Get list of currently attached NVMe volumes
		echo "Getting list of attached NVMe volumes..."
		ATTACHED_VOLUMES=$(lsblk --json | jq -r '.blockdevices[] | select(.name | startswith("nvme")) | select(.children != null) | .children[] | select(.name | startswith("nvme")) | .name')
		echo "Currently attached volumes: $ATTACHED_VOLUMES"

		# Wait for the device to settle after attachment
		echo "Waiting 30 seconds for device to settle..."
		sleep 30

		# Get updated list of NVMe volumes and find the last attached one
		NEW_VOLUMES=$(lsblk --json | jq -r '.blockdevices[] | select(.name | startswith("nvme")) | select(.children != null) | .children[] | select(.name | startswith("nvme")) | .name')
		CORRECT_DEVICE=$(echo "$NEW_VOLUMES" | tail -n 3 | head -n 1)

		if [ -z "$CORRECT_DEVICE" ]; then
			echo "Failed to find the correct device"
			echo "Available devices:"
			lsblk --json | jq .
			exit 1
		fi

		echo "Using device: $CORRECT_DEVICE"

		# Create mount directory if it doesn't exist
		echo "Creating mount directory..."
		sudo mkdir -p %s
		if [ $? -ne 0 ]; then
			echo "Failed to create mount directory"
			exit 1
		fi

		# Mount the volume
		echo "Mounting /dev/$CORRECT_DEVICE to %s..."
		sudo mount /dev/$CORRECT_DEVICE %s
		if [ $? -ne 0 ]; then
			echo "Mount command failed"
			echo "Mount error details:"
			dmesg | tail -n 50
			exit 1
		fi

		# Verify the mount was successful
		echo "Verifying mount..."
		if ! ls %s > /dev/null; then
			echo "Failed to verify mount or directory is empty"
			exit 1
		fi

		echo "Successfully mounted volume to %s"
		echo "Contents of mount path:"
		ls -la %s
	`, mountPath, mountPath, mountPath, mountPath, mountPath, mountPath, mountPath, mountPath, mountPath, mountPath, mountPath, mountPath)

	return s.ssmOps.RunCommand(ctx, instanceID, mountCmd, 300)
}

// unmountVolume unmounts the volume
func (s *Scanner) unmountVolume(ctx context.Context, instanceID, mountPath string) error {
	unmountCmd := fmt.Sprintf(`
		set -x  # Enable command tracing
		set -e  # Exit on error
		
		# Unmount the volume
		if mountpoint -q %s; then
			echo "Unmounting %s..."
			sudo umount %s
			if [ $? -ne 0 ]; then
				echo "Failed to unmount volume"
				echo "Current processes using the mount:"
				sudo lsof %s || true
				exit 1
			fi
		else
			echo "Mount point %s is not mounted"
		fi
		
		# Remove mount directory
		if [ -d %s ]; then
			echo "Removing mount directory..."
			sudo rmdir %s
			if [ $? -ne 0 ]; then
				echo "Failed to remove mount directory"
				ls -la %s
				exit 1
			fi
		fi
		
		echo "Successfully cleaned up %s"
	`, mountPath, mountPath, mountPath, mountPath, mountPath, mountPath, mountPath, mountPath, mountPath)

	return s.ssmOps.RunCommand(ctx, instanceID, unmountCmd, 300)
}

// runScan runs Trufflehog on the mounted volume
func (s *Scanner) runScan(ctx context.Context, instanceID, mountPath, pillagePath string, jsonOutput bool) error {
	// First install Trufflehog
	if err := s.installTrufflehog(ctx, instanceID); err != nil {
		return fmt.Errorf("failed to install Trufflehog: %w", err)
	}

	// Then run the scan
	searchPath := filepath.Join(mountPath, pillagePath)
	jsonFlag := ""
	if jsonOutput {
		jsonFlag = "--json"
	}

	scanCmd := fmt.Sprintf(`
		set -x  # Enable command tracing
		set -e  # Exit on error
		
		# Verify mount point exists and is mounted
		if ! mountpoint -q %s; then
			echo "Mount point %s is not mounted"
			echo "Current mounts:"
			mount
			exit 1
		fi
		
		# Check if search path exists
		SEARCH_PATH="%s"
		if [ ! -d "$SEARCH_PATH" ]; then
			echo "Search path $SEARCH_PATH does not exist"
			echo "Contents of mount point:"
			ls -la %s
			exit 1
		fi
		
		# Run Trufflehog scan and save output
		echo "Running Trufflehog scan on $SEARCH_PATH"
		cd "$SEARCH_PATH"
		/tmp/trufflehog filesystem --no-verification --concurrency=5 %s . > /tmp/trufflehog.out
		
		# Check if output file was created and has content
		if [ ! -f /tmp/trufflehog.out ]; then
			echo "Trufflehog output file was not created"
			exit 1
		fi

		# Print file size and first few lines for verification
		echo "Trufflehog output file size:"
		ls -l /tmp/trufflehog.out
		echo "First few lines of output:"
		head -n 5 /tmp/trufflehog.out
	`, mountPath, mountPath, searchPath, mountPath, jsonFlag)

	return s.ssmOps.RunCommand(ctx, instanceID, scanCmd, 3600)
}

// SaveOutput saves the scan output to a file
func (s *Scanner) SaveOutput(ctx context.Context, instanceID, outFile string) error {
	// Get the command output from the instance
	getOutputCmd := `
		set -x  # Enable command tracing
		set -e  # Exit on error

		if [ ! -f /tmp/trufflehog.out ]; then
			echo "Trufflehog output file not found"
			exit 1
		fi

		cat /tmp/trufflehog.out
	`

	// Run command to get output
	if err := s.ssmOps.RunCommand(ctx, instanceID, getOutputCmd, 300); err != nil {
		return fmt.Errorf("failed to get Trufflehog output: %w", err)
	}

	return nil
}

func (s *Scanner) installTrufflehog(ctx context.Context, instanceID string) error {
	setupCmd := `
		set -x  # Enable command tracing
		set -e  # Exit on error

		# First check if Trufflehog is already installed
		if command -v /tmp/trufflehog > /dev/null; then
			echo "Trufflehog is already installed"
			exit 0
		fi

		# Install required packages
		if command -v apt-get > /dev/null 2>&1; then
			sudo apt-get update
			sudo apt-get install -y wget
		elif command -v yum > /dev/null 2>&1; then
			sudo yum install -y wget
		else
			echo "No supported package manager found"
			exit 1
		fi

		# Download and install Trufflehog binary
		wget https://github.com/trufflesecurity/trufflehog/releases/download/v3.75.0/trufflehog_3.75.0_linux_amd64.tar.gz -O /tmp/trufflehog.tar.gz
		tar xzf /tmp/trufflehog.tar.gz -C /tmp/
		chmod +x /tmp/trufflehog

		# Verify installation
		if ! command -v /tmp/trufflehog > /dev/null; then
			echo "Trufflehog installation failed"
			exit 1
		fi

		echo "Trufflehog installed successfully"
	`

	return s.ssmOps.RunCommand(ctx, instanceID, setupCmd, 600)
} 