#!/usr/bin/env bash
# ECS on EC2: one t3.micro container instance, one booking task (sole capacity writer). Safe to re-run.
set -euo pipefail
source "$(dirname "$0")/env.sh"
trust() { echo "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"$1\"},\"Action\":\"sts:AssumeRole\"}]}"; }
role() {  # name service
  aws iam get-role --role-name "$1" >/dev/null 2>&1 || { aws iam create-role --role-name "$1" --assume-role-policy-document "$(trust "$2")" >/dev/null; sleep 8; }
}

log "Wait for RDS, then store DATABASE_URL"
aws rds wait db-instance-available --db-instance-identifier "$DB_ID"
EP=$(aws rds describe-db-instances --db-instance-identifier "$DB_ID" --query 'DBInstances[0].Endpoint.Address' --output text)
PW=$(aws ssm get-parameter --name "/$P/DB_PASSWORD" --with-decryption --query Parameter.Value --output text)
aws ssm put-parameter --name "/$P/DATABASE_URL" --type SecureString --overwrite \
  --value "postgresql://setuhaul_admin:$PW@$EP:5432/setuhaul?sslmode=require" >/dev/null
echo "RDS endpoint $EP"

log "IAM: instance role, task execution role, task role"
role "$P-ecs-instance-role" ec2.amazonaws.com
aws iam attach-role-policy --role-name "$P-ecs-instance-role" --policy-arn arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role
aws iam attach-role-policy --role-name "$P-ecs-instance-role" --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
if ! aws iam get-instance-profile --instance-profile-name "$P-ecs-instance" >/dev/null 2>&1; then
  aws iam create-instance-profile --instance-profile-name "$P-ecs-instance" >/dev/null
  aws iam add-role-to-instance-profile --instance-profile-name "$P-ecs-instance" --role-name "$P-ecs-instance-role"
  sleep 10
fi
role "$P-task-exec-role" ecs-tasks.amazonaws.com
aws iam attach-role-policy --role-name "$P-task-exec-role" --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam put-role-policy --role-name "$P-task-exec-role" --policy-name ssm-secrets --policy-document \
  "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"ssm:GetParameters\",\"Resource\":\"arn:aws:ssm:$AWS_REGION:$ACCOUNT_ID:parameter/$P/*\"}]}"
role "$P-task-role" ecs-tasks.amazonaws.com
aws iam put-role-policy --role-name "$P-task-role" --policy-name read-snapshot --policy-document \
  "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"s3:GetObject\",\"Resource\":\"arn:aws:s3:::$BUCKET/snapshot/*\"}]}"

log "Log group + cluster"
aws logs create-log-group --log-group-name "$LOG_GROUP" 2>/dev/null || true
aws logs put-retention-policy --log-group-name "$LOG_GROUP" --retention-in-days 7
aws ecs create-cluster --cluster-name "$CLUSTER" >/dev/null

log "Container instance (t3.micro, ECS-optimized AL2023, public IP, IMDSv2)"
IID=$(aws ec2 describe-instances --filters Name=tag:Name,Values="$P-ecs" Name=instance-state-name,Values=pending,running,stopped,stopping \
      --query 'Reservations[0].Instances[0].InstanceId' --output text)
if [ "$IID" = "None" ]; then
  AMI=$(aws ssm get-parameter --name /aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id --query Parameter.Value --output text)
  SG=$(aws ec2 describe-security-groups --filters Name=group-name,Values="$P-booking-sg" --query 'SecurityGroups[0].GroupId' --output text)
  printf '#!/bin/bash\necho ECS_CLUSTER=%s >> /etc/ecs/ecs.config\n' "$CLUSTER" > "$TMPD/userdata.sh"
  IID=$(aws ec2 run-instances --image-id "$AMI" --instance-type t3.micro --security-group-ids "$SG" \
        --iam-instance-profile Name="$P-ecs-instance" --user-data "file://$TMPD/userdata.sh" \
        --metadata-options HttpTokens=required,HttpEndpoint=enabled \
        --credit-specification CpuCredits=standard \
        --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$P-ecs},{Key=project,Value=$P}]" \
        --query 'Instances[0].InstanceId' --output text)
fi
aws ec2 start-instances --instance-ids "$IID" >/dev/null 2>&1 || true
aws ec2 wait instance-running --instance-ids "$IID"
echo "instance $IID"

log "Task definition (no volumes: stateless; secrets from SSM; snapshot from S3)"
IMAGE="$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$ECR_REPO:latest"
cat > "$TMPD/taskdef.json" <<EOF
{"family":"$SERVICE","networkMode":"bridge","requiresCompatibilities":["EC2"],
 "executionRoleArn":"arn:aws:iam::$ACCOUNT_ID:role/$P-task-exec-role",
 "taskRoleArn":"arn:aws:iam::$ACCOUNT_ID:role/$P-task-role",
 "containerDefinitions":[{"name":"booking","image":"$IMAGE","essential":true,
   "memory":700,"memoryReservation":450,
   "portMappings":[{"containerPort":8000,"hostPort":8000,"protocol":"tcp"}],
   "environment":[{"name":"SNAPSHOT_URI","value":"s3://$BUCKET/$SNAPSHOT_KEY"},
                  {"name":"AWS_REGION","value":"$AWS_REGION"},
                  {"name":"SIM_START","value":"2026-08-04T10:00:00+05:30"}],
   "secrets":[{"name":"DATABASE_URL","valueFrom":"arn:aws:ssm:$AWS_REGION:$ACCOUNT_ID:parameter/$P/DATABASE_URL"},
              {"name":"REDIS_URL","valueFrom":"arn:aws:ssm:$AWS_REGION:$ACCOUNT_ID:parameter/$P/REDIS_URL"}],
   "logConfiguration":{"logDriver":"awslogs","options":{"awslogs-group":"$LOG_GROUP","awslogs-region":"$AWS_REGION","awslogs-stream-prefix":"booking"}}}]}
EOF
TD=$(aws ecs register-task-definition --cli-input-json "file://$TMPD/taskdef.json" --query taskDefinition.taskDefinitionArn --output text)
echo "$TD"

log "Service: exactly one task; never two writers during a deploy"
if aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" --query 'services[?status==`ACTIVE`].serviceName' --output text | grep -q "$SERVICE"; then
  aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --task-definition "$TD" --desired-count 1 \
    --deployment-configuration minimumHealthyPercent=0,maximumPercent=100 --force-new-deployment >/dev/null
else
  aws ecs create-service --cluster "$CLUSTER" --service-name "$SERVICE" --task-definition "$TD" --desired-count 1 \
    --launch-type EC2 --deployment-configuration minimumHealthyPercent=0,maximumPercent=100 >/dev/null
fi
PUB=$(aws ec2 describe-instances --instance-ids "$IID" --query 'Reservations[0].Instances[0].PublicDnsName' --output text)
echo "Booking API (once the task is running): http://$PUB:8000/health" | tee "$TMPD/endpoint.txt"
