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

		# Wait for the device to settle after attachment
		echo "Waiting 10 seconds for device to settle..."
		sleep 10

		# Force kernel to re-read partition table
		echo "Forcing kernel to re-read partition table..."
		sudo partprobe || true

		# Show initial device state
		echo "Initial block device state:"
		sudo lsblk -o NAME,SERIAL,TYPE,SIZE,MOUNTPOINT,LABEL
		sudo blkid

		# Function to convert xvd device to potential nvme device
		get_nvme_device() {
			local xvd_name="$1"
			local device_letter="${xvd_name#/dev/xvd}"
			local index=14  # Default to 14 for 'bo'
			echo "/dev/nvme${index}n1"
		}

		# Try to find the actual device
		DEVICE_NAME=""
		EXPECTED_NVME=$(get_nvme_device "%s")
		echo "Looking for device: %s (expected NVMe: $EXPECTED_NVME)"

		# First try the original device name
		if [ -b "%s" ]; then
			DEVICE_NAME="%s"
		# Then try the expected NVMe name
		elif [ -b "$EXPECTED_NVME" ]; then
			DEVICE_NAME="$EXPECTED_NVME"
		else
			# List all block devices if we can't find it directly
			echo "Device not found directly, checking all devices..."
			for dev in /dev/nvme*n1; do
				if [ -b "$dev" ]; then
					echo "Found block device: $dev"
					DEVICE_NAME="$dev"
					break
				fi
			done
		fi

		if [ -z "$DEVICE_NAME" ]; then
			echo "Failed to find device"
			echo "Available devices:"
			ls -l /dev/nvme*n1 2>/dev/null || true
			echo "Device details:"
			sudo lsblk -J | jq .
			exit 1
		fi

		echo "Using device: $DEVICE_NAME"

		# Check if device exists
		if [ ! -b "$DEVICE_NAME" ]; then
			echo "Device $DEVICE_NAME does not exist"
			echo "Available devices:"
			ls -l /dev/nvme*n1 2>/dev/null || true
			echo "Kernel messages:"
			dmesg | tail -n 50
			exit 1
		fi

		# Wait for partitions to appear
		echo "Waiting for partitions to appear..."
		for i in {1..10}; do
			if [ -b "${DEVICE_NAME}p1" ]; then
				echo "Found partition ${DEVICE_NAME}p1"
				DEVICE_NAME="${DEVICE_NAME}p1"
				break
			fi
			echo "Attempt $i: Partition not found, waiting..."
			sleep 1
		done

		if [ ! -b "$DEVICE_NAME" ]; then
			echo "Failed to find partition after 10 attempts"
			echo "Available devices:"
			ls -l /dev/nvme*n1* 2>/dev/null || true
			exit 1
		fi

		MOUNT_PATH="%s"

		# Check if mount point exists and is mounted
		if mountpoint -q "$MOUNT_PATH"; then
			echo "Mount point $MOUNT_PATH is already mounted, unmounting first..."
			sudo umount "$MOUNT_PATH" || true
		fi

		# Create mount directory if it doesn't exist
		sudo mkdir -p "$MOUNT_PATH"
		if [ $? -ne 0 ]; then
			echo "Failed to create mount directory"
			exit 1
		fi

		# Get filesystem details before mounting
		echo "Filesystem details for $DEVICE_NAME:"
		sudo blkid "$DEVICE_NAME" || true
		sudo file -sL "$DEVICE_NAME" || true

		# Try to mount with different options
		echo "Attempting to mount $DEVICE_NAME to $MOUNT_PATH"
		if ! sudo mount -t ext4 -o ro "$DEVICE_NAME" "$MOUNT_PATH"; then
			echo "First mount attempt failed, trying without specifying filesystem type..."
			if ! sudo mount -o ro "$DEVICE_NAME" "$MOUNT_PATH"; then
				echo "Both mount attempts failed"
				echo "Mount error details:"
				sudo dmesg | tail -n 50
				echo "Filesystem details:"
				sudo blkid "$DEVICE_NAME" || true
				sudo file -sL "$DEVICE_NAME" || true
				exit 1
			fi
		fi

		# Verify mount was successful
		if ! mountpoint -q "$MOUNT_PATH"; then
			echo "Mount verification failed"
			echo "Current mounts:"
			mount | grep "$MOUNT_PATH"
			exit 1
		fi

		echo "Successfully mounted $DEVICE_NAME to $MOUNT_PATH"
		df -h "$MOUNT_PATH"
		sudo ls -la "$MOUNT_PATH"
	`, deviceName, deviceName, deviceName, deviceName, mountPath)

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