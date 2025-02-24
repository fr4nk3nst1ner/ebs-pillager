package utils

import (
	"fmt"

	"ec2bandit/internal/config"
)

// ValidateConfig validates the application configuration
func ValidateConfig(cfg *config.Config) error {
	if cfg.ListEBS || cfg.ListEC2 {
		return validateListingConfig(cfg)
	}

	if cfg.Pillage {
		return validatePillageConfig(cfg)
	}

	return fmt.Errorf("no valid operation specified")
}

func validateListingConfig(cfg *config.Config) error {
	if cfg.ListEBS {
		if cfg.DstProfile == "" {
			return fmt.Errorf("destination profile is required for listing EBS volumes")
		}
		if cfg.DstRegion == "" {
			return fmt.Errorf("destination region is required for listing EBS volumes")
		}
	}

	if cfg.ListEC2 {
		if cfg.SrcProfile == "" {
			return fmt.Errorf("source profile is required for listing EC2 instances")
		}
		if cfg.SrcRegion == "" {
			return fmt.Errorf("source region is required for listing EC2 instances")
		}
	}

	return nil
}

func validatePillageConfig(cfg *config.Config) error {
	if cfg.MountHost == "" {
		return fmt.Errorf("mount host is required for pillaging")
	}

	if cfg.TargetEC2 == "" {
		return fmt.Errorf("target EC2 instance is required for pillaging")
	}

	if cfg.MountPath == "" {
		return fmt.Errorf("mount path is required for pillaging")
	}

	if cfg.SrcProfile == "" || cfg.SrcRegion == "" {
		return fmt.Errorf("source profile and region are required for pillaging")
	}

	if cfg.DstProfile == "" || cfg.DstRegion == "" {
		return fmt.Errorf("destination profile and region are required for pillaging")
	}

	return nil
} 