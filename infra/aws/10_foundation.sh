#!/usr/bin/env bash
# Budget, private data bucket, security groups, RDS Postgres. Safe to re-run.
set -euo pipefail
source "$(dirname "$0")/env.sh"

log "Budget: \$20/month gross usage (credits excluded), email alerts 50/80/100% + forecast"
if ! aws budgets describe-budget --account-id "$ACCOUNT_ID" --budget-name "$P-20usd" >/dev/null 2>&1; then
  : "${BUDGET_EMAIL:?set BUDGET_EMAIL}"
  cat > "$TMPD/budget.json" <<EOF
{"BudgetName":"$P-20usd","BudgetLimit":{"Amount":"20","Unit":"USD"},"TimeUnit":"MONTHLY","BudgetType":"COST",
 "CostTypes":{"IncludeCredit":false,"IncludeRefund":false,"IncludeTax":true,"IncludeSubscription":true,"IncludeUpfront":true,
 "IncludeRecurring":true,"IncludeOtherSubscription":true,"IncludeSupport":true,"IncludeDiscount":true,"UseBlended":false,"UseAmortized":false}}
EOF
  n() { echo "{\"Notification\":{\"NotificationType\":\"$1\",\"ComparisonOperator\":\"GREATER_THAN\",\"Threshold\":$2,\"ThresholdType\":\"PERCENTAGE\"},\"Subscribers\":[{\"SubscriptionType\":\"EMAIL\",\"Address\":\"$BUDGET_EMAIL\"}]}"; }
  echo "[$(n ACTUAL 50),$(n ACTUAL 80),$(n ACTUAL 100),$(n FORECASTED 100)]" > "$TMPD/notif.json"
  aws budgets create-budget --account-id "$ACCOUNT_ID" --budget "file://$TMPD/budget.json" \
    --notifications-with-subscribers "file://$TMPD/notif.json"
fi

log "S3 bucket $BUCKET (private, versioned) + snapshot upload"
aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null || aws s3api create-bucket --bucket "$BUCKET" >/dev/null
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3 sync "$REPO_ROOT/data/" "s3://$BUCKET/snapshot/2026-08-04/" --only-show-errors
aws s3 ls "s3://$BUCKET/snapshot/2026-08-04/"

log "Security groups"
VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
sg() {  # name description -> id
  local id
  id=$(aws ec2 describe-security-groups --filters Name=group-name,Values="$1" Name=vpc-id,Values="$VPC_ID" \
       --query 'SecurityGroups[0].GroupId' --output text)
  if [ "$id" = "None" ]; then
    id=$(aws ec2 create-security-group --group-name "$1" --description "$2" --vpc-id "$VPC_ID" --query GroupId --output text)
  fi
  echo "$id"
}
BOOKING_SG=$(sg "$P-booking-sg" "SetuHaul booking service")
RDS_SG=$(sg "$P-rds-sg" "SetuHaul RDS Postgres: booking service only")
MY_IP="${MY_IP:-$(curl -s https://checkip.amazonaws.com | tr -d '[:space:]')}"
aws ec2 authorize-security-group-ingress --group-id "$BOOKING_SG" --protocol tcp --port 8000 --cidr "$MY_IP/32" 2>/dev/null || true
aws ec2 authorize-security-group-ingress --group-id "$RDS_SG" --protocol tcp --port 5432 --source-group "$BOOKING_SG" 2>/dev/null || true
echo "booking-sg=$BOOKING_SG (8000 from $MY_IP/32)  rds-sg=$RDS_SG (5432 from booking-sg only)"

log "RDS Postgres 16 (db.t4g.micro, 20 GB gp3, private, single-AZ)"
if ! aws rds describe-db-instances --db-instance-identifier "$DB_ID" >/dev/null 2>&1; then
  if ! aws ssm get-parameter --name "/$P/DB_PASSWORD" >/dev/null 2>&1; then
    PW=$(python -c "import secrets,string;print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(32)))")
    aws ssm put-parameter --name "/$P/DB_PASSWORD" --type SecureString --value "$PW" >/dev/null
  fi
  PW=$(aws ssm get-parameter --name "/$P/DB_PASSWORD" --with-decryption --query Parameter.Value --output text)
  aws rds create-db-instance --db-instance-identifier "$DB_ID" --engine postgres --engine-version 16.15 \
    --db-instance-class db.t4g.micro --allocated-storage 20 --storage-type gp3 \
    --master-username setuhaul_admin --master-user-password "$PW" \
    --vpc-security-group-ids "$RDS_SG" --no-publicly-accessible --no-multi-az \
    --backup-retention-period 1 --storage-encrypted --no-deletion-protection \
    --tags Key=project,Value=$P >/dev/null
  echo "RDS creation started (about 10 minutes)"
else
  aws rds describe-db-instances --db-instance-identifier "$DB_ID" --query 'DBInstances[0].DBInstanceStatus' --output text
fi
