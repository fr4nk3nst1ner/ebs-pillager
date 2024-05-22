aws --profile jstinesse --region us-east-1 iam create-policy --policy-name SSMInstancePolicy --policy-document file://ssm_policy.json
aws --profile jstinesse --region us-east-1 iam create-instance-profile --instance-profile-name SSMInstanceProfile
aws --profile jstinesse --region us-east-1 iam create-role --role-name SSMInstanceRole --assume-role-policy-document file://ssm_trust_policy.json
aws --profile jstinesse --region us-east-1 iam add-role-to-instance-profile --instance-profile-name SSMInstanceProfile --role-name SSMInstancePolicy
aws --profile jstinesse --region us-east-1 iam attach-role-policy --role-name SSMInstanceRole --policy-arn arn:a
ws:iam::768242378223:policy/SSMInstancePolicy
aws --profile jstinesse --region us-east-1 iam create-instance-profile --instance-profile-name SSMInstanceProfile
aws --profile jstinesse --region us-east-1 iam add-role-to-instance-profile --instance-profile-name SSMInstanceProfile --role-name SSMInstanceRole
