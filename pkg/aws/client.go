package aws

import (
	"context"

	awsconfig "github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	"github.com/aws/aws-sdk-go-v2/service/kms"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
	"github.com/aws/aws-sdk-go-v2/service/sts"
	
	"ec2bandit/internal/config"
)

// Client represents a wrapper for AWS service clients
type Client struct {
	EC2 *ec2.Client
	KMS *kms.Client
	SSM *ssm.Client
	STS *sts.Client
}

// Services holds both source and destination AWS service clients
type Services struct {
	SrcClient *Client
	DstClient *Client
	EBSOps    *EBSOperations
	SSMOps    *SSMOperations
}

// NewServices creates AWS service clients from the provided configuration
func NewServices(cfg *config.Config) (*Services, error) {
	ctx := context.Background()
	
	srcClient, err := NewClient(ctx, cfg.SrcRegion, cfg.SrcProfile)
	if err != nil {
		return nil, err
	}

	dstClient, err := NewClient(ctx, cfg.DstRegion, cfg.DstProfile)
	if err != nil {
		return nil, err
	}

	services := &Services{
		SrcClient: srcClient,
		DstClient: dstClient,
	}

	services.EBSOps = NewEBSOperations(srcClient, dstClient, cfg)
	services.SSMOps = NewSSMOperations(dstClient)

	return services, nil
}

// NewClient creates a new AWS service client wrapper
func NewClient(ctx context.Context, region, profile string) (*Client, error) {
	cfg, err := awsconfig.LoadDefaultConfig(ctx,
		awsconfig.WithRegion(region),
		awsconfig.WithSharedConfigProfile(profile),
	)
	if err != nil {
		return nil, err
	}

	return &Client{
		EC2: ec2.NewFromConfig(cfg),
		KMS: kms.NewFromConfig(cfg),
		SSM: ssm.NewFromConfig(cfg),
		STS: sts.NewFromConfig(cfg),
	}, nil
} 