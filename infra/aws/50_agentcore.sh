#!/usr/bin/env bash
# Agent on Amazon Bedrock AgentCore Runtime: ARM64 image built by CodeBuild, execution role,
# runtime with Cognito JWT inbound auth (audience = the web client). Safe to re-run (updates the runtime).
set -euo pipefail
source "$(dirname "$0")/env.sh"
source "$TMPD/cognito.env"
source "$TMPD/web.env"
AREPO="$P-agent"
RT_NAME="${P}_agent"

log "ECR $AREPO"
aws ecr describe-repositories --repository-names "$AREPO" >/dev/null 2>&1 || \
  aws ecr create-repository --repository-name "$AREPO" --image-scanning-configuration scanOnPush=true >/dev/null
aws ecr put-lifecycle-policy --repository-name "$AREPO" --lifecycle-policy-text \
  '{"rules":[{"rulePriority":1,"selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":5},"action":{"type":"expire"}}]}' >/dev/null

log "CodeBuild project $P-agent-build (ARM64)"
python - "$TMPD" "$ACCOUNT_ID" "$AWS_REGION" "$GIT_URL" "$P" <<'PY'
import json, sys
tmpd, acct, region, url, p = sys.argv[1:]
proj = {
  "name": f"{p}-agent-build",
  "source": {"type": "GITHUB", "location": url, "buildspec": open(f"{tmpd}/buildspec.yml").read(), "gitCloneDepth": 1},
  "sourceVersion": "main", "artifacts": {"type": "NO_ARTIFACTS"},
  "environment": {"type": "ARM_CONTAINER", "image": "aws/codebuild/amazonlinux2-aarch64-standard:3.0",
                  "computeType": "BUILD_GENERAL1_SMALL", "privilegedMode": True,
                  "environmentVariables": [
                      {"name": "REGISTRY", "value": f"{acct}.dkr.ecr.{region}.amazonaws.com"},
                      {"name": "IMAGE_REPO", "value": f"{p}-agent"},
                      {"name": "DOCKERFILE", "value": "agent/Dockerfile"}]},
  "serviceRole": f"arn:aws:iam::{acct}:role/{p}-codebuild-role", "timeoutInMinutes": 25,
  "logsConfig": {"cloudWatchLogs": {"status": "ENABLED", "groupName": f"/{p}/codebuild"}},
}
json.dump(proj, open(f"{tmpd}/codebuild-agent.json", "w"))
PY
if aws codebuild batch-get-projects --names "$P-agent-build" --query 'projects[0].name' --output text | grep -q "$P-agent-build"; then
  aws codebuild update-project --cli-input-json "file://$TMPD/codebuild-agent.json" >/dev/null
else
  aws codebuild create-project --cli-input-json "file://$TMPD/codebuild-agent.json" >/dev/null
fi
log "Build agent image"
B=$(aws codebuild start-build --project-name "$P-agent-build" --query build.id --output text)
while :; do
  S=$(aws codebuild batch-get-builds --ids "$B" --query 'builds[0].buildStatus' --output text)
  [ "$S" = "IN_PROGRESS" ] || break
  sleep 15
done
echo "build $S"
[ "$S" = "SUCCEEDED" ] || exit 1
DIGEST=$(aws ecr describe-images --repository-name "$AREPO" --image-ids imageTag=latest --query 'imageDetails[0].imageDigest' --output text)
IMAGE="$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$AREPO@$DIGEST"   # pin by digest so updates roll

log "Execution role"
ROLE="$P-agentcore-role"
if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",
    \"Principal\":{\"Service\":\"bedrock-agentcore.amazonaws.com\"},\"Action\":\"sts:AssumeRole\",
    \"Condition\":{\"StringEquals\":{\"aws:SourceAccount\":\"$ACCOUNT_ID\"}}}]}" >/dev/null
  sleep 10
fi
cat > "$TMPD/agentcore-policy.json" <<EOF
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["ecr:BatchGetImage","ecr:GetDownloadUrlForLayer"],"Resource":"arn:aws:ecr:$AWS_REGION:$ACCOUNT_ID:repository/$AREPO"},
 {"Effect":"Allow","Action":"ecr:GetAuthorizationToken","Resource":"*"},
 {"Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents","logs:DescribeLogStreams","logs:DescribeLogGroups"],"Resource":"*"},
 {"Effect":"Allow","Action":["xray:PutTraceSegments","xray:PutTelemetryRecords","xray:GetSamplingRules","xray:GetSamplingTargets"],"Resource":"*"},
 {"Effect":"Allow","Action":"cloudwatch:PutMetricData","Resource":"*","Condition":{"StringEquals":{"cloudwatch:namespace":"bedrock-agentcore"}}},
 {"Effect":"Allow","Action":["bedrock-agentcore:GetWorkloadAccessToken","bedrock-agentcore:GetWorkloadAccessTokenForJWT","bedrock-agentcore:GetWorkloadAccessTokenForUserId"],"Resource":"*"},
 {"Effect":"Allow","Action":"ssm:GetParameters","Resource":"arn:aws:ssm:$AWS_REGION:$ACCOUNT_ID:parameter/$P/*"}]}
EOF
aws iam put-role-policy --role-name "$ROLE" --policy-name agentcore --policy-document "file://$TMPD/agentcore-policy.json"
sleep 8

MODEL=$(grep -E '^OPEN_ROUTER_MODEL=' "$REPO_ROOT/.env" | cut -d= -f2- | tr -d '\r')
python - "$TMPD/agentcore.json" "$RT_NAME" "$IMAGE" "$ACCOUNT_ID" "$ROLE" "$AWS_REGION" "$COGNITO_USER_POOL_ID" \
  "$COGNITO_CLIENT_ID" "https://$WEB_DOMAIN/api" "${MODEL:-anthropic/claude-sonnet-4.5}" "$P" <<'PY'
import json, sys
out, name, image, acct, role, region, pool, client, api, model, p = sys.argv[1:]
cfg = {
  "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": image}},
  "roleArn": f"arn:aws:iam::{acct}:role/{role}",
  "networkConfiguration": {"networkMode": "PUBLIC"},
  "protocolConfiguration": {"serverProtocol": "HTTP"},
  "authorizerConfiguration": {"customJWTAuthorizer": {
      "discoveryUrl": f"https://cognito-idp.{region}.amazonaws.com/{pool}/.well-known/openid-configuration",
      "allowedAudience": [client]}},
  "environmentVariables": {"BOOKING_API_URL": api, "SETUHAUL_SSM_PREFIX": f"/{p}", "AWS_REGION": region,
                           "OPEN_ROUTER_MODEL": model, "LANGSMITH_TRACING": "true", "LANGSMITH_PROJECT": p},
  "description": "SetuHaul driver conversation agent (LangChain v1)",
}
json.dump(cfg, open(out, "w"))
PY

RT_ID=$(aws bedrock-agentcore-control list-agent-runtimes --query "agentRuntimes[?agentRuntimeName=='$RT_NAME'].agentRuntimeId | [0]" --output text)
if [ "$RT_ID" = "None" ] || [ -z "$RT_ID" ]; then
  log "Create runtime $RT_NAME"
  python -c "import json,sys;d=json.load(open(sys.argv[1]));d['agentRuntimeName']=sys.argv[2];json.dump(d,open(sys.argv[1],'w'))" \
    "$TMPD/agentcore.json" "$RT_NAME"
  RT_ID=$(aws bedrock-agentcore-control create-agent-runtime --cli-input-json "file://$TMPD/agentcore.json" --query agentRuntimeId --output text)
else
  log "Update runtime $RT_ID"
  aws bedrock-agentcore-control update-agent-runtime --agent-runtime-id "$RT_ID" --cli-input-json "file://$TMPD/agentcore.json" >/dev/null
fi
while :; do
  ST=$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$RT_ID" --query status --output text)
  case "$ST" in READY) break ;; *FAILED*) echo "runtime $ST"; exit 1 ;; *) sleep 10 ;; esac
done
ARN=$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$RT_ID" --query agentRuntimeArn --output text)
printf 'AGENT_RUNTIME_ID=%s\nAGENT_RUNTIME_ARN=%s\n' "$RT_ID" "$ARN" > "$TMPD/agentcore.env"
echo "runtime READY: $ARN"
