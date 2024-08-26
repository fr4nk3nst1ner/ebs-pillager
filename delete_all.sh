#!/bin/bash

# Function to terminate EC2 instances with the "TrufflehogTesting" tag
terminate_instances() {
    local profile=$1
    local region=$2
    local tag_key="Name"
    local tag_value="TrufflehogTesting"

    # Get running EC2 instance IDs with the specified tag
    instance_ids=$(aws --profile "$profile" --region "$region" ec2 describe-instances \
      --filters "Name=tag:$tag_key,Values=$tag_value" "Name=instance-state-name,Values=running" \
      --query 'Reservations[].Instances[].InstanceId' \
      --output text)

    # Check if instance ID is empty (no running instances with the specified tag)
    if [ -z "$instance_ids" ]; then
      echo "No running instances with the tag $tag_key=$tag_value found. Exiting script."
      exit 1
    fi

    # Stop and terminate EC2 instances
    echo "Stopping and terminating EC2 instance(s) with tag $tag_key=$tag_value: $instance_ids"
    aws --profile "$profile" --region "$region" ec2 stop-instances --instance-ids $instance_ids
    aws --profile "$profile" --region "$region" ec2 wait instance-stopped --instance-ids $instance_ids
    aws --profile "$profile" --region "$region" ec2 terminate-instances --instance-ids $instance_ids
    echo "EC2 instance(s) $instance_ids with tag $tag_key=$tag_value stopped and terminated."
}

# Function to delete EBS volumes with the "TrufflehogTesting" tag
delete_volumes() {
    local profile=$1
    local region=$2
    local tag_key="Name"
    local tag_value="TrufflehogTesting"

    # Get EBS volume IDs with the specified tag
    volume_ids=$(aws ec2 describe-volumes \
        --profile "$profile" \
        --region "$region" \
        --filters "Name=tag:$tag_key,Values=$tag_value" \
        --query 'Volumes[*].VolumeId' \
        --output text)

    # Check if volume ID is empty (no volumes with the specified tag)
    if [ -z "$volume_ids" ]; then
      echo "No volumes with the tag $tag_key=$tag_value found. Exiting script."
      exit 1
    fi

    # Determine the root volume by checking the block devices
    root_volume=$(aws ec2 describe-instances \
        --profile "$profile" \
        --region "$region" \
        --query 'Reservations[].Instances[].BlockDeviceMappings[?DeviceName==`/dev/xvda`].Ebs.VolumeId' \
        --output text)

    # Loop through each volume and delete it, skipping the root filesystem volume
    for volume_id in $volume_ids; do
        if [ "$volume_id" == "$root_volume" ]; then
            echo "Skipping root filesystem volume $volume_id"
            continue
        fi
        
        echo "Deleting volume $volume_id..."
        aws ec2 delete-volume \
            --profile "$profile" \
            --region "$region" \
            --volume-id "$volume_id"
        if [ $? -ne 0 ]; then
            echo "Failed to delete volume $volume_id"
        else
            echo "Deleted volume $volume_id"
        fi
    done
    echo "All non-root EBS volumes with the tag $tag_key=$tag_value have been deleted."
}

# Function to delete snapshots with the "TrufflehogTesting" tag owned by your account
delete_snapshots() {
    local profile=$1
    local region=$2
    local account_id=$(aws --profile "$profile" --region "$region" sts get-caller-identity --query 'Account' --output text)
    local tag_key="Name"
    local tag_value="TrufflehogTesting"

    # Get snapshot IDs with the specified tag owned by your account in the region
    snapshot_ids=$(aws ec2 describe-snapshots \
        --profile "$profile" \
        --region "$region" \
        --owner-ids "$account_id" \
        --filters "Name=tag:$tag_key,Values=$tag_value" \
        --query 'Snapshots[*].SnapshotId' \
        --output text)

    # Check if snapshot ID is empty (no snapshots with the specified tag)
    if [ -z "$snapshot_ids" ]; then
      echo "No snapshots with the tag $tag_key=$tag_value found. Exiting script."
      exit 1
    fi

    # Loop through each snapshot and delete it
    for snapshot_id in $snapshot_ids; do
        echo "Deleting snapshot $snapshot_id..."
        aws ec2 delete-snapshot \
            --profile "$profile" \
            --region "$region" \
            --snapshot-id "$snapshot_id"
        if [ $? -ne 0 ]; then
            echo "Failed to delete snapshot $snapshot_id"
            echo "Error: $(aws ec2 describe-snapshots --profile "$profile" --region "$region" --snapshot-ids "$snapshot_id" --query 'Snapshots[0].StateMessage' --output text)"
        else
            echo "Deleted snapshot $snapshot_id"
        fi
    done
    echo "All snapshots with the tag $tag_key=$tag_value have been deleted."
}

# Check if profile and region arguments are provided
if [[ $# -ne 4 || ($1 != "--profile" && $1 != "--region") ]]; then
    echo "Usage: $0 --profile PROFILENAME --region REGION"
    exit 1
fi

# Parse arguments
while [[ $# -gt 0 ]]; do
    key="$1"
    case $key in
        --profile)
            profile_name="$2"
            shift # past argument
            shift # past value
            ;;
        --region)
            region="$2"
            shift # past argument
            shift # past value
            ;;
        *)
            echo "Unknown option: $key"
            exit 1
            ;;
    esac
done

echo "Select operation:"
echo "1. Terminate running EC2 instances with the 'TrufflehogTesting' tag"
echo "2. Delete EBS volumes with the 'TrufflehogTesting' tag"
echo "3. Delete snapshots with the 'TrufflehogTesting' tag"
read -p "Enter choice [1-3]: " choice

case $choice in
    1)
        terminate_instances "$profile_name" "$region"
        ;;
    2)
        delete_volumes "$profile_name" "$region"
        ;;
    3)
        delete_snapshots "$profile_name" "$region"
        ;;
    *)
        echo "Invalid choice."
        exit 1
        ;;
esac
