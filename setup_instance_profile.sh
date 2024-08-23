#!/bin/bash

set -e

# Function to display usage information
usage() {
    echo "Usage: $0 --profile PROFILE [--create | --delete]"
    exit 1
}

# Check if jq is installed
if ! command -v jq &> /dev/null; then
    echo "jq is required but it's not installed. Please install jq and try again."
    exit 1
fi

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --profile) PROFILE="$2"; shift ;;
        --create) ACTION="create" ;;
        --delete) ACTION="delete" ;;
        *) usage ;;
    esac
    shift
done

# Validate arguments
if [ -z "$PROFILE" ] || [ -z "$ACTION" ]; then
    usage
fi

# Get the AWS account ID dynamically
ACCOUNT_ID=$(aws sts get-caller-identity --profile "$PROFILE" --query "Account" --output text)

# IAM policy documents
SSM_POLICY_JSON=$(cat <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "ssm:DescribeInstanceInformation",
                "ssm:ListAssociations",
                "ssm:ListInstanceAssociations",
                "ssm:ListCommandInvocations",
                "ssm:ListCommands",
                "ssm:SendCommand",
                "ssm:UpdateInstanceInformation",
                "ssm:GetMessages",
                "ec2messages:GetMessages",
                "ssmmessages:CreateControlChannel",
                "ec2messages:AcknowledgeMessage",
                "ec2messages:SendReply"
            ],
            "Resource": "*"
        }
    ]
}
EOF
)

SSM_TRUST_POLICY_JSON=$(cat <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {
                "Service": "ec2.amazonaws.com"
            },
            "Action": "sts:AssumeRole"
        }
    ]
}
EOF
)

# Check if a policy exists
policy_exists() {
    aws --profile "$PROFILE" --region us-east-1 iam get-policy --policy-arn "arn:aws:iam::$ACCOUNT_ID:policy/SSMInstancePolicy" &> /dev/null
}

# Check if a role exists
role_exists() {
    aws --profile "$PROFILE" --region us-east-1 iam get-role --role-name SSMInstanceRole &> /dev/null
}

# Check if an instance profile exists
instance_profile_exists() {
    aws --profile "$PROFILE" --region us-east-1 iam get-instance-profile --instance-profile-name SSMInstanceProfile &> /dev/null
}

# Create policies and instance profile
create_instance_profile() {
    if policy_exists; then
        echo "Policy SSMInstancePolicy already exists."
    else
        echo "Creating SSM policy..."
        echo "$SSM_POLICY_JSON" > ssm_policy.json
        aws --profile "$PROFILE" --region us-east-1 iam create-policy --policy-name SSMInstancePolicy --policy-document file://ssm_policy.json
    fi

    if instance_profile_exists; then
        echo "Instance profile SSMInstanceProfile already exists."
    else
        echo "Creating instance profile..."
        aws --profile "$PROFILE" --region us-east-1 iam create-instance-profile --instance-profile-name SSMInstanceProfile
    fi

    if role_exists; then
        echo "Role SSMInstanceRole already exists."
    else
        echo "Creating role..."
        echo "$SSM_TRUST_POLICY_JSON" > ssm_trust_policy.json
        aws --profile "$PROFILE" --region us-east-1 iam create-role --role-name SSMInstanceRole --assume-role-policy-document file://ssm_trust_policy.json

        echo "Adding role to instance profile..."
        aws --profile "$PROFILE" --region us-east-1 iam add-role-to-instance-profile --instance-profile-name SSMInstanceProfile --role-name SSMInstanceRole
    fi

    echo "Attaching policy to role..."
    aws --profile "$PROFILE" --region us-east-1 iam attach-role-policy --role-name SSMInstanceRole --policy-arn "arn:aws:iam::$ACCOUNT_ID:policy/SSMInstancePolicy"
}

# Delete policies and instance profile
delete_instance_profile() {
    if role_exists; then
        echo "Detaching policy from role..."
        aws --profile "$PROFILE" --region us-east-1 iam detach-role-policy --role-name SSMInstanceRole --policy-arn "arn:aws:iam::$ACCOUNT_ID:policy/SSMInstancePolicy"

        echo "Removing role from instance profile..."
        aws --profile "$PROFILE" --region us-east-1 iam remove-role-from-instance-profile --instance-profile-name SSMInstanceProfile --role-name SSMInstanceRole

        echo "Deleting role..."
        aws --profile "$PROFILE" --region us-east-1 iam delete-role --role-name SSMInstanceRole
    else
        echo "Role SSMInstanceRole does not exist."
    fi

    if instance_profile_exists; then
        echo "Deleting instance profile..."
        aws --profile "$PROFILE" --region us-east-1 iam delete-instance-profile --instance-profile-name SSMInstanceProfile
    else
        echo "Instance profile SSMInstanceProfile does not exist."
    fi

    if policy_exists; then
        echo "Deleting policy..."
        aws --profile "$PROFILE" --region us-east-1 iam delete-policy --policy-arn "arn:aws:iam::$ACCOUNT_ID:policy/SSMInstancePolicy"
    else
        echo "Policy SSMInstancePolicy does not exist."
    fi
}

# Perform the requested action
if [ "$ACTION" == "create" ]; then
    create_instance_profile
elif [ "$ACTION" == "delete" ]; then
    delete_instance_profile
else
    usage
fi

