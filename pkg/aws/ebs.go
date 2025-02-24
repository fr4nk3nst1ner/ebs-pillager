package aws

import (
	"context"
	"fmt"
	"log"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	"github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"ec2bandit/internal/config"
	"ec2bandit/pkg/utils"
)

// EBSOperations handles EBS volume operations
type EBSOperations struct {
	srcClient *Client
	dstClient *Client
	cfg       *config.Config
}

// NewEBSOperations creates a new EBS operations handler
func NewEBSOperations(srcClient, dstClient *Client, cfg *config.Config) *EBSOperations {
	return &EBSOperations{
		srcClient: srcClient,
		dstClient: dstClient,
		cfg:       cfg,
	}
}

// ListVolumes lists all available EBS volumes
func (e *EBSOperations) ListVolumes(ctx context.Context) error {
	volumesResp, err := e.dstClient.EC2.DescribeVolumes(ctx, &ec2.DescribeVolumesInput{})
	if err != nil {
		return fmt.Errorf("failed to describe volumes: %w", err)
	}

	for _, volume := range volumesResp.Volumes {
		if volume.State == types.VolumeStateAvailable {
			log.Printf("EBS Volume ID: %s, State: %s, Size: %d GiB, Snapshot ID: %s\n",
				*volume.VolumeId,
				volume.State,
				volume.Size,
				aws.ToString(volume.SnapshotId))
		}
	}

	return nil
}

// ListSnapshots lists all available snapshots
func (e *EBSOperations) ListSnapshots(ctx context.Context) error {
	snapshotsResp, err := e.dstClient.EC2.DescribeSnapshots(ctx, &ec2.DescribeSnapshotsInput{
		OwnerIds: []string{"self"},
	})
	if err != nil {
		return fmt.Errorf("failed to describe snapshots: %w", err)
	}

	for _, snapshot := range snapshotsResp.Snapshots {
		log.Printf("Snapshot ID: %s, Volume ID: %s, State: %s, Start Time: %s\n",
			*snapshot.SnapshotId,
			*snapshot.VolumeId,
			snapshot.State,
			snapshot.StartTime.String())
	}

	return nil
}

// CreateSnapshot creates a snapshot of the root volume of the specified instance
func (e *EBSOperations) CreateSnapshot(ctx context.Context, instanceID string) (string, string, error) {
	// Get the instance details to find the root volume
	resp, err := e.srcClient.EC2.DescribeInstances(ctx, &ec2.DescribeInstancesInput{
		InstanceIds: []string{instanceID},
	})
	if err != nil {
		return "", "", fmt.Errorf("failed to describe instance: %w", err)
	}

	if len(resp.Reservations) == 0 || len(resp.Reservations[0].Instances) == 0 {
		return "", "", fmt.Errorf("instance %s not found", instanceID)
	}

	instance := resp.Reservations[0].Instances[0]
	var rootVolume *types.InstanceBlockDeviceMapping

	// Find the root volume
	for _, mapping := range instance.BlockDeviceMappings {
		if *mapping.DeviceName == *instance.RootDeviceName {
			rootVolume = &mapping
			break
		}
	}

	if rootVolume == nil || rootVolume.Ebs == nil {
		return "", "", fmt.Errorf("root volume not found for instance %s", instanceID)
	}

	// Create snapshot of the root volume
	snapResp, err := e.srcClient.EC2.CreateSnapshot(ctx, &ec2.CreateSnapshotInput{
		VolumeId: rootVolume.Ebs.VolumeId,
		Description: aws.String(fmt.Sprintf("Snapshot of root volume for instance %s", instanceID)),
		TagSpecifications: []types.TagSpecification{
			{
				ResourceType: types.ResourceTypeSnapshot,
				Tags: []types.Tag{
					{
						Key:   aws.String("Name"),
						Value: aws.String(fmt.Sprintf("ec2bandit-snapshot-%s", instanceID)),
					},
				},
			},
		},
	})
	if err != nil {
		return "", "", fmt.Errorf("failed to create snapshot: %w", err)
	}

	// Wait for snapshot to be available
	if err := e.WaitForSnapshotAvailable(ctx, *snapResp.SnapshotId); err != nil {
		return "", "", fmt.Errorf("failed waiting for snapshot to be available: %w", err)
	}

	return *snapResp.SnapshotId, aws.ToString(snapResp.KmsKeyId), nil
}

// ShareSnapshot shares a snapshot with another account
func (e *EBSOperations) ShareSnapshot(ctx context.Context, snapshotID, accountID string) error {
	input := &ec2.ModifySnapshotAttributeInput{
		SnapshotId: aws.String(snapshotID),
		Attribute:  types.SnapshotAttributeNameCreateVolumePermission,
		CreateVolumePermission: &types.CreateVolumePermissionModifications{
			Add: []types.CreateVolumePermission{
				{
					UserId: aws.String(accountID),
				},
			},
		},
	}

	_, err := e.srcClient.EC2.ModifySnapshotAttribute(ctx, input)
	if err != nil {
		return fmt.Errorf("failed to share snapshot: %w", err)
	}

	return nil
}

// WaitForSnapshotAvailable waits for a snapshot to become available
func (e *EBSOperations) WaitForSnapshotAvailable(ctx context.Context, snapshotID string) error {
	utils.Debug("Waiting for snapshot %s to become available...", snapshotID)
	
	maxAttempts := 60 // 10 minutes with 10-second intervals
	for attempt := 0; attempt < maxAttempts; attempt++ {
		resp, err := e.srcClient.EC2.DescribeSnapshots(ctx, &ec2.DescribeSnapshotsInput{
			SnapshotIds: []string{snapshotID},
		})
		if err != nil {
			return fmt.Errorf("failed to describe snapshot: %w", err)
		}

		if len(resp.Snapshots) == 0 {
			return fmt.Errorf("snapshot %s not found", snapshotID)
		}

		snapshot := resp.Snapshots[0]
		utils.Debug("Snapshot state: %s", snapshot.State)

		if snapshot.State == types.SnapshotStateCompleted {
			return nil
		} else if snapshot.State == types.SnapshotStateError {
			return fmt.Errorf("snapshot entered error state")
		}

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(10 * time.Second):
			// Continue waiting
		}
	}

	return fmt.Errorf("timed out waiting for snapshot to become available")
}

// CreateVolumeFromSnapshot creates a new volume from a snapshot
func (e *EBSOperations) CreateVolumeFromSnapshot(ctx context.Context, snapshotID, availabilityZone string) (string, error) {
	// Wait for snapshot to be available before creating volume
	if err := e.WaitForSnapshotAvailable(ctx, snapshotID); err != nil {
		return "", fmt.Errorf("failed waiting for snapshot to be available: %w", err)
	}

	input := &ec2.CreateVolumeInput{
		SnapshotId:       aws.String(snapshotID),
		AvailabilityZone: aws.String(availabilityZone),
	}

	resp, err := e.dstClient.EC2.CreateVolume(ctx, input)
	if err != nil {
		return "", fmt.Errorf("failed to create volume: %w", err)
	}

	// Wait for volume to be available
	waiter := ec2.NewVolumeAvailableWaiter(e.dstClient.EC2)
	if err := waiter.Wait(ctx, &ec2.DescribeVolumesInput{
		VolumeIds: []string{*resp.VolumeId},
	}, 5*time.Minute); err != nil {
		return "", fmt.Errorf("failed waiting for volume to be available: %w", err)
	}

	return *resp.VolumeId, nil
}

// DeleteSnapshotAndVolume deletes the specified snapshot and volume
func (e *EBSOperations) DeleteSnapshotAndVolume(ctx context.Context, srcSnapshotID, dstSnapshotID, volumeID string) error {
	if volumeID != "" {
		_, err := e.dstClient.EC2.DeleteVolume(ctx, &ec2.DeleteVolumeInput{
			VolumeId: aws.String(volumeID),
		})
		if err != nil {
			log.Printf("Warning: failed to delete volume %s: %v", volumeID, err)
		}
	}

	if srcSnapshotID != "" {
		_, err := e.srcClient.EC2.DeleteSnapshot(ctx, &ec2.DeleteSnapshotInput{
			SnapshotId: aws.String(srcSnapshotID),
		})
		if err != nil {
			log.Printf("Warning: failed to delete source snapshot %s: %v", srcSnapshotID, err)
		}
	}

	if dstSnapshotID != "" && dstSnapshotID != srcSnapshotID {
		_, err := e.dstClient.EC2.DeleteSnapshot(ctx, &ec2.DeleteSnapshotInput{
			SnapshotId: aws.String(dstSnapshotID),
		})
		if err != nil {
			log.Printf("Warning: failed to delete destination snapshot %s: %v", dstSnapshotID, err)
		}
	}

	return nil
}

// getNextAvailableDevice finds the next available device name for volume attachment
func (e *EBSOperations) getNextAvailableDevice(ctx context.Context, instanceID string) (string, error) {
	// Get current instance block device mappings
	instanceResp, err := e.dstClient.EC2.DescribeInstances(ctx, &ec2.DescribeInstancesInput{
		InstanceIds: []string{instanceID},
	})
	if err != nil {
		return "", fmt.Errorf("failed to describe instance: %w", err)
	}

	if len(instanceResp.Reservations) == 0 || len(instanceResp.Reservations[0].Instances) == 0 {
		return "", fmt.Errorf("instance %s not found", instanceID)
	}

	// Get list of used device names
	usedDevices := make(map[string]bool)
	for _, mapping := range instanceResp.Reservations[0].Instances[0].BlockDeviceMappings {
		usedDevices[*mapping.DeviceName] = true
	}

	// Try device names from xvdf to xvdz, then xvdba to xvdbz, then xvdca to xvdcz
	devicePrefixes := []string{"xvd", "xvdb", "xvdc"}
	for _, prefix := range devicePrefixes {
		for c := 'a'; c <= 'z'; c++ {
			// Skip 'a' to 'e' for the first prefix as they're typically reserved
			if prefix == "xvd" && c <= 'e' {
				continue
			}
			deviceName := fmt.Sprintf("/dev/%s%c", prefix, c)
			if !usedDevices[deviceName] {
				return deviceName, nil
			}
		}
	}

	return "", fmt.Errorf("no available device names found")
}

// VerifyVolumeAttachment verifies that a volume is attached and returns its actual device name
func (e *EBSOperations) VerifyVolumeAttachment(ctx context.Context, volumeID, instanceID string) (string, error) {
	input := &ec2.DescribeVolumesInput{
		VolumeIds: []string{volumeID},
	}

	result, err := e.dstClient.EC2.DescribeVolumes(ctx, input)
	if err != nil {
		return "", fmt.Errorf("failed to describe volume: %w", err)
	}

	if len(result.Volumes) == 0 {
		return "", fmt.Errorf("volume %s not found", volumeID)
	}

	volume := result.Volumes[0]
	if len(volume.Attachments) == 0 {
		return "", fmt.Errorf("volume %s is not attached", volumeID)
	}

	for _, attachment := range volume.Attachments {
		if *attachment.InstanceId == instanceID {
			return *attachment.Device, nil
		}
	}

	return "", fmt.Errorf("volume %s is not attached to instance %s", volumeID, instanceID)
}

// AttachVolume attaches a volume to an instance
func (e *EBSOperations) AttachVolume(ctx context.Context, volumeID, instanceID, device string) (string, error) {
	// Find next available device name
	deviceName, err := e.getNextAvailableDevice(ctx, instanceID)
	if err != nil {
		return "", fmt.Errorf("failed to find available device name: %w", err)
	}

	input := &ec2.AttachVolumeInput{
		VolumeId:   aws.String(volumeID),
		InstanceId: aws.String(instanceID),
		Device:     aws.String(deviceName),
	}

	_, err = e.dstClient.EC2.AttachVolume(ctx, input)
	if err != nil {
		return "", fmt.Errorf("failed to attach volume: %w", err)
	}

	// Wait for volume to be attached
	waiter := ec2.NewVolumeInUseWaiter(e.dstClient.EC2)
	err = waiter.Wait(ctx, &ec2.DescribeVolumesInput{
		VolumeIds: []string{volumeID},
	}, 5*time.Minute)

	if err != nil {
		return "", fmt.Errorf("failed waiting for volume to be attached: %w", err)
	}

	// Verify attachment and get actual device name
	actualDevice, err := e.VerifyVolumeAttachment(ctx, volumeID, instanceID)
	if err != nil {
		return "", fmt.Errorf("failed to verify volume attachment: %w", err)
	}

	return actualDevice, nil
}

// DetachVolume detaches a volume from an instance
func (e *EBSOperations) DetachVolume(ctx context.Context, volumeID string) error {
	input := &ec2.DetachVolumeInput{
		VolumeId: aws.String(volumeID),
	}

	_, err := e.dstClient.EC2.DetachVolume(ctx, input)
	if err != nil {
		return fmt.Errorf("failed to detach volume: %w", err)
	}

	// Wait for volume to be detached
	waiter := ec2.NewVolumeAvailableWaiter(e.dstClient.EC2)
	err = waiter.Wait(ctx, &ec2.DescribeVolumesInput{
		VolumeIds: []string{volumeID},
	}, 5*time.Minute)

	if err != nil {
		return fmt.Errorf("failed waiting for volume to be detached: %w", err)
	}

	return nil
} 