#!/bin/bash

# Function to terminate EC2 instances
terminate_instances() {
    local profile=$1
    local region=$2

    # Get running EC2 instance ID from specified profile and region
    instance_ids=$(aws --profile "$profile" --region "$region" ec2 describe-instances \
      --query 'Reservations[].Instances[?State.Name==`running`].InstanceId' \
      --output text)

    # Check if instance ID is empty (no running instances)
    if [ -z "$instance_ids" ]; then
      echo "No running instances found. Exiting script."
      exit 1
    fi

    # Stop and terminate EC2 instances
    echo "Stopping and terminating EC2 instance(s): $instance_ids"
    aws --profile "$profile" --region "$region" ec2 stop-instances --instance-ids $instance_ids
    aws --profile "$profile" --region "$region" ec2 wait instance-stopped --instance-ids $instance_ids
    aws --profile "$profile" --region "$region" ec2 terminate-instances --instance-ids $instance_ids
    echo "EC2 instance(s) $instance_ids stopped and terminated."
}

# Function to delete EBS volumes
delete_volumes() {
    local profile=$1
    local region=$2

    # Get all EBS volume IDs in the region
    volume_ids=$(aws ec2 describe-volumes \
        --profile "$profile" \
        --region "$region" \
        --query 'Volumes[*].VolumeId' \
        --output text)

    # Loop through each volume and delete it along with its snapshots
    for volume_id in $volume_ids; do
        echo "Deleting volume $volume_id..."

        # Get all snapshots associated with the volume
        snapshot_ids=$(aws ec2 describe-snapshots \
            --profile "$profile" \
            --region "$region" \
            --filters "Name=volume-id,Values=$volume_id" \
            --query 'Snapshots[*].SnapshotId' \
            --output text)

        # Loop through each snapshot and delete it
        for snapshot_id in $snapshot_ids; do
            echo "Deleting snapshot $snapshot_id..."
            aws ec2 delete-snapshot \
                --profile "$profile" \
                --region "$region" \
                --snapshot-id "$snapshot_id"
        done

        # Delete the volume
        aws ec2 delete-volume \
            --profile "$profile" \
            --region "$region" \
            --volume-id "$volume_id"
    done
    echo "All EBS volumes and their snapshots have been deleted."
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
echo "1. Terminate running EC2 instances"
echo "2. Delete EBS volumes and snapshots"
read -p "Enter choice [1-2]: " choice

case $choice in
    1)
        terminate_instances "$profile_name" "$region"
        ;;
    2)
        delete_volumes "$profile_name" "$region"
        ;;
    *)
        echo "Invalid choice."
        exit 1
        ;;
esac
