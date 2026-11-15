#!/usr/bin/env bash
# Pause the paid pieces when you're not testing: ECS task -> 0, EC2 stopped, RDS stopped.
# Data is kept (RDS storage, S3). Note: AWS restarts a stopped RDS instance after 7 days.
set -euo pipefail
source "$(dirname "$0")/env.sh"
log "Scale service to 0"
aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --desired-count 0 >/dev/null || true
IID=$(aws ec2 describe-instances --filters Name=tag:Name,Values="$P-ecs" Name=instance-state-name,Values=running,pending \
      --query 'Reservations[0].Instances[0].InstanceId' --output text)
if [ "$IID" != "None" ]; then log "Stop EC2 $IID"; aws ec2 stop-instances --instance-ids "$IID" >/dev/null; fi
ST=$(aws rds describe-db-instances --db-instance-identifier "$DB_ID" --query 'DBInstances[0].DBInstanceStatus' --output text)
if [ "$ST" = "available" ]; then log "Stop RDS $DB_ID"; aws rds stop-db-instance --db-instance-identifier "$DB_ID" >/dev/null; else echo "RDS is $ST"; fi
echo "Stopped. While stopped you pay only for storage (~\$3/month for RDS + EBS)."
