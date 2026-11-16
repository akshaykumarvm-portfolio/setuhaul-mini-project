#!/usr/bin/env bash
# Cognito user pool (Lite tier, free up to 10k MAU), groups, web client, demo users; agent service key. Safe to re-run.
set -euo pipefail
source "$(dirname "$0")/env.sh"

log "User pool"
POOL_ID=$(aws cognito-idp list-user-pools --max-results 60 --query "UserPools[?Name=='$P-users'].Id | [0]" --output text)
if [ "$POOL_ID" = "None" ]; then
  POOL_ID=$(aws cognito-idp create-user-pool --pool-name "$P-users" --user-pool-tier LITE \
    --policies 'PasswordPolicy={MinimumLength=10,RequireUppercase=true,RequireLowercase=true,RequireNumbers=true,RequireSymbols=false}' \
    --admin-create-user-config AllowAdminCreateUserOnly=true \
    --schema Name=driver_id,AttributeDataType=String,Mutable=true Name=facility_id,AttributeDataType=String,Mutable=true \
    --deletion-protection INACTIVE --query UserPool.Id --output text)
fi
echo "pool $POOL_ID"

log "Web app client (public SPA: no secret; USER_PASSWORD_AUTH over TLS + refresh)"
CLIENT_ID=$(aws cognito-idp list-user-pool-clients --user-pool-id "$POOL_ID" --query "UserPoolClients[?ClientName=='$P-web'].ClientId | [0]" --output text)
if [ "$CLIENT_ID" = "None" ]; then
  CLIENT_ID=$(aws cognito-idp create-user-pool-client --user-pool-id "$POOL_ID" --client-name "$P-web" --no-generate-secret \
    --explicit-auth-flows ALLOW_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH \
    --id-token-validity 60 --access-token-validity 60 --refresh-token-validity 12 \
    --token-validity-units IdToken=minutes,AccessToken=minutes,RefreshToken=hours \
    --prevent-user-existence-errors ENABLED --query UserPoolClient.ClientId --output text)
fi
echo "client $CLIENT_ID"

log "Groups"
for g in driver coordinator planner; do
  aws cognito-idp create-group --user-pool-id "$POOL_ID" --group-name "$g" >/dev/null 2>&1 || true
done

log "Demo password (SSM /$P/DEMO_PASSWORD) and agent service key (SSM /$P/AGENT_API_KEY)"
gen() { python -c "import secrets,string;a=string.ascii_letters+string.digits;print('Sh'+''.join(secrets.choice(a) for _ in range($1))+'7k')"; }
aws ssm get-parameter --name "/$P/DEMO_PASSWORD" >/dev/null 2>&1 || \
  aws ssm put-parameter --name "/$P/DEMO_PASSWORD" --type SecureString --value "$(gen 10)" >/dev/null
aws ssm get-parameter --name "/$P/AGENT_API_KEY" >/dev/null 2>&1 || \
  aws ssm put-parameter --name "/$P/AGENT_API_KEY" --type SecureString --value "$(gen 40)" >/dev/null
PW=$(aws ssm get-parameter --name "/$P/DEMO_PASSWORD" --with-decryption --query Parameter.Value --output text)

user() {  # username group [attr=value ...]
  local u=$1 g=$2; shift 2
  local attrs=()
  for kv in "$@"; do attrs+=("Name=custom:${kv%%=*},Value=${kv#*=}"); done
  if ! aws cognito-idp admin-get-user --user-pool-id "$POOL_ID" --username "$u" >/dev/null 2>&1; then
    aws cognito-idp admin-create-user --user-pool-id "$POOL_ID" --username "$u" --message-action SUPPRESS \
      ${attrs[@]:+--user-attributes "${attrs[@]}"} >/dev/null
  fi
  aws cognito-idp admin-set-user-password --user-pool-id "$POOL_ID" --username "$u" --password "$PW" --permanent
  aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL_ID" --username "$u" --group-name "$g"
  echo "  $u ($g${*:+, $*})"
}
log "Demo users"
for d in DRV006 DRV013 DRV004 DRV014 DRV012 DRV001 DRV015 DRV008; do
  user "$(echo "$d" | tr 'A-Z' 'a-z')" driver "driver_id=$d"
done
user neha.jaipur coordinator facility_id=FAC-JAI-01
user rohit.gurugram coordinator facility_id=FAC-GGN-01
user planner planner

cat > "$TMPD/cognito.env" <<EOF
COGNITO_USER_POOL_ID=$POOL_ID
COGNITO_CLIENT_ID=$CLIENT_ID
EOF
{
  echo "SetuHaul demo sign-ins (password is the same for all; stored in SSM /$P/DEMO_PASSWORD)"
  echo "password: $PW"
  echo "drivers: drv006 drv013 drv004 drv014 drv012 drv001 drv015 drv008"
  echo "coordinators: neha.jaipur (Jaipur DC), rohit.gurugram (Gurugram DC)"
  echo "planner: planner"
} > "$TMPD/demo_users.txt"
echo "wrote $TMPD/cognito.env and $TMPD/demo_users.txt (gitignored)"
