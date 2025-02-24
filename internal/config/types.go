package config

import "time"

// Config holds the application configuration
type Config struct {
	SrcProfile  string
	DstProfile  string
	SrcRegion   string
	DstRegion   string
	ListEBS     bool
	ListEC2     bool
	MountHost   string
	Pillage     bool
	TargetEC2   string
	PillagePath string
	MountPath   string
	SSHKeyPath  string
	Retain      bool
	JSON        bool    // Enable JSON output format
	OutFile     string
	Debug       bool    // Enable debug logging
	NoBanner    bool    // Disable banner display
	ShowExamples bool   // Show example commands
}

// VolumeDetails represents information about an EBS volume
type VolumeDetails struct {
	VolumeID   string `json:"VolumeId"`
	State      string `json:"State"`
	Size       int32  `json:"Size"`
	SnapshotID string `json:"SnapshotId"`
}

// SnapshotDetails represents information about an EBS snapshot
type SnapshotDetails struct {
	SnapshotID string    `json:"SnapshotId"`
	VolumeID   string    `json:"VolumeId"`
	State      string    `json:"State"`
	StartTime  time.Time `json:"StartTime"`
}

// InstanceDetails represents information about an EC2 instance
type InstanceDetails struct {
	InstanceID       string `json:"InstanceId"`
	State           string `json:"State"`
	PublicIPAddress  string `json:"PublicIpAddress"`
	PrivateIPAddress string `json:"PrivateIpAddress"`
} 