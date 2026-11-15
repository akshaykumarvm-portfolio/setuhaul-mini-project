#!/usr/bin/env bash
# Resume after stop.sh: RDS -> EC2 -> one booking task. Prints the new endpoint (public DNS changes on restart).
set -euo pipefail
source "$(dirname "$0")/env.sh"
ST=$(aws rds describe-db-instances --db-instance-identifier "$DB_ID" --query 'DBInstances[0].DBInstanceStatus' --output text)
[ "$ST" = "stopped" ] && { log "Start RDS"; aws rds start-db-instance --db-instance-identifier "$DB_ID" >/dev/null; }
IID=$(aws ec2 describe-instances --filters Name=tag:Name,Values="$P-ecs" Name=instance-state-name,Values=stopped,stopping,running,pending \
      --query 'Reservations[0].Instances[0].InstanceId' --output text)
log "Start EC2 $IID"
aws ec2 wait instance-stopped --instance-ids "$IID" 2>/dev/null || true
aws ec2 start-instances --instance-ids "$IID" >/dev/null
log "Wait for RDS and EC2"
aws rds wait db-instance-available --db-instance-identifier "$DB_ID"
aws ec2 wait instance-running --instance-ids "$IID"
aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --desired-count 1 >/dev/null
PUB=$(aws ec2 describe-instances --instance-ids "$IID" --query 'Reservations[0].Instances[0].PublicDnsName' --output text)
echo "Booking API: http://$PUB:8000/health  (allow 1-2 minutes for the task to start)" | tee "$TMPD/endpoint.txt"
