#!/usr/bin/env bash
# Open a private tunnel to RDS through the booking service's EC2 instance (AWS Systems Manager port forwarding).
# RDS stays private: no public endpoint, no security-group change, no SSH key. Needs the Session Manager plugin:
# https://s3.amazonaws.com/session-manager-downloads/plugin/latest/windows/SessionManagerPluginSetup.exe
#
#   bash infra/aws/db_tunnel.sh            # keep this window open; Ctrl+C closes the tunnel
#   then connect any Postgres client to localhost:15432 (details printed below)
set -euo pipefail
source "$(dirname "$0")/env.sh"
LOCAL_PORT="${LOCAL_PORT:-15432}"

IID=$(aws ec2 describe-instances --filters Name=tag:Name,Values="$P-ecs" Name=instance-state-name,Values=running \
      --query 'Reservations[0].Instances[0].InstanceId' --output text)
[ "$IID" != "None" ] || { echo "EC2 instance is not running: bash infra/aws/start.sh first"; exit 1; }
EP=$(aws rds describe-db-instances --db-instance-identifier "$DB_ID" --query 'DBInstances[0].Endpoint.Address' --output text)

cat <<EOF

  Tunnel: localhost:$LOCAL_PORT  ->  $IID  ->  $EP:5432
  Database: setuhaul    User: setuhaul_admin    SSL: require
  Password: MSYS_NO_PATHCONV=1 aws ssm get-parameter --name /$P/DB_PASSWORD --with-decryption --query Parameter.Value --output text

  psql (in the setuhaul conda env):
    psql "host=localhost port=$LOCAL_PORT dbname=setuhaul user=setuhaul_admin sslmode=require"
  GUI (DBeaver / pgAdmin / VS Code): host localhost, port $LOCAL_PORT, same user, password and SSL.

EOF
aws ssm start-session --target "$IID" --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"$EP\"],\"portNumber\":[\"5432\"],\"localPortNumber\":[\"$LOCAL_PORT\"]}"
