#!/bin/bash

# Default values
profile=""
region=""
instance_profile=""
ip_allowlist=""
key_name=""
image_id=""
instance_type=""
security_group_id=""

# Function to display usage
usage() {
    echo "Usage: $0 --profile PROFILENAME --region REGION --instance-profile INSTANCEPROFILE --ip-allowlist IPADDRESS --key KEYNAME --image-id AMIIMAGEID --instance-type INSTANCETYPE"
    exit 1
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    key="$1"
    case $key in
        --profile)
            profile="$2"
            shift # past argument
            shift # past value
            ;;
        --region)
            region="$2"
            shift # past argument
            shift # past value
            ;;
        --instance-profile)
            instance_profile="$2"
            shift # past argument
            shift # past value
            ;;
        --ip-allowlist)
            ip_allowlist="$2"
            shift # past argument
            shift # past value
            ;;
        --key)
            key_name="$2"
            shift # past argument
            shift # past value
            ;;
        --image-id)
            image_id="$2"
            shift # past argument
            shift # past value
            ;;
        --instance-type)
            instance_type="$2"
            shift # past argument
            shift # past value
            ;;
        *)
            echo "Unknown option: $key"
            usage
            ;;
    esac
done

# Check if all required arguments are provided
if [[ -z "$profile" || -z "$region" || -z "$instance_profile" || -z "$ip_allowlist" || -z "$key_name" || -z "$image_id" || -z "$instance_type" ]]; then
    echo "Error: Missing required arguments."
    usage
fi

# Launch the EC2 instance and retrieve its InstanceId, Public IP, and Security Group ID
instance_info=$(aws ec2 run-instances \
    --profile "$profile" \
    --region "$region" \
    --image-id "$image_id" \
    --instance-type "$instance_type" \
    --key-name "$key_name" \
    --iam-instance-profile Name="$instance_profile" \
    --query 'Instances[0].[InstanceId,PublicIpAddress,SecurityGroups[0].GroupId]' \
    --output json)

# Extract instance ID, public IP, and security group ID from the instance information
instance_id=$(echo "$instance_info" | jq -r '.[0]')
public_ip=$(echo "$instance_info" | jq -r '.[1]')
security_group_id=$(echo "$instance_info" | jq -r '.[2]')

# Check if the instance ID was obtained successfully
if [[ -n "$instance_id" && -n "$security_group_id" ]]; then
    echo "Instance launched with ID: $instance_id"
    echo "Security Group ID: $security_group_id"

    # Authorize port 22 for the specified IP address
    aws ec2 authorize-security-group-ingress \
        --profile "$profile" \
        --region "$region" \
        --group-id "$security_group_id" \
        --protocol tcp \
        --port 22 \
        --cidr "$ip_allowlist"

    echo "Port 22 (SSH) authorized for IP address $ip_allowlist."

    # Start the instance
    aws ec2 start-instances --profile "$profile" --region "$region" --instance-ids "$instance_id"
    echo "Instance started successfully."

    # Wait for the instance to be running
    aws ec2 wait instance-running --profile "$profile" --region "$region" --instance-ids "$instance_id"

    # Get the external IP of the instance
    external_ip=$(aws ec2 describe-instances \
        --profile "$profile" \
        --region "$region" \
        --instance-ids "$instance_id" \
        --query 'Reservations[0].Instances[0].PublicIpAddress' \
        --output text)

    echo "External IP of the instance: $external_ip"
else
    echo "Failed to launch the instance or obtain security group ID."
fi
