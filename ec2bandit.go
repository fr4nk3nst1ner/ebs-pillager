package main

import (
	"os"

	"ec2bandit/cmd/ec2bandit"
	"ec2bandit/pkg/utils"
)

func main() {
	if err := ec2bandit.Execute(); err != nil {
		utils.Error("Application failed: %v", err)
		os.Exit(1)
	}
} 