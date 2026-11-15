#!/usr/bin/env bash
# App secrets in SSM, ECR repo, CodeBuild project that builds the booking image from GitHub. Safe to re-run.
set -euo pipefail
source "$(dirname "$0")/env.sh"

log "App secrets -> SSM Parameter Store (SecureString) from .env (values never printed)"
put_secret() {  # name value
  [ -n "$2" ] && aws ssm put-parameter --name "/$P/$1" --type SecureString --value "$2" --overwrite >/dev/null && echo "  /$P/$1"
}
getenv() { grep -E "^$1=" "$REPO_ROOT/.env" | head -1 | cut -d= -f2- | tr -d '\r'; }
put_secret REDIS_URL "$(getenv REDIS_URL)"
put_secret OPEN_ROUTER_API_KEY "$(getenv OPEN_ROUTER_API_KEY)"
put_secret LANGSMITH_API_KEY "$(getenv LANGSMITH_API_KEY)"

log "ECR repository $ECR_REPO"
aws ecr describe-repositories --repository-names "$ECR_REPO" >/dev/null 2>&1 || \
  aws ecr create-repository --repository-name "$ECR_REPO" --image-scanning-configuration scanOnPush=true >/dev/null
# keep only the 5 newest images (storage stays inside the free 500 MB)
aws ecr put-lifecycle-policy --repository-name "$ECR_REPO" --lifecycle-policy-text \
  '{"rules":[{"rulePriority":1,"selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":5},"action":{"type":"expire"}}]}' >/dev/null

log "CodeBuild service role"
ROLE=$P-codebuild-role
if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"codebuild.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  sleep 10
fi
cat > "$TMPD/codebuild-policy.json" <<EOF
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],"Resource":"*"},
 {"Effect":"Allow","Action":"ecr:GetAuthorizationToken","Resource":"*"},
 {"Effect":"Allow","Action":["ecr:BatchCheckLayerAvailability","ecr:CompleteLayerUpload","ecr:InitiateLayerUpload",
   "ecr:PutImage","ecr:UploadLayerPart","ecr:BatchGetImage","ecr:GetDownloadUrlForLayer"],
  "Resource":"arn:aws:ecr:$AWS_REGION:$ACCOUNT_ID:repository/$P-*"}]}
EOF
aws iam put-role-policy --role-name "$ROLE" --policy-name build --policy-document "file://$TMPD/codebuild-policy.json"

log "CodeBuild project $P-build (public GitHub source, Docker build, push to ECR)"
cat > "$TMPD/buildspec.yml" <<'EOF'
version: 0.2
phases:
  pre_build:
    commands:
      - aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $REGISTRY
      - TAG=$(echo $CODEBUILD_RESOLVED_SOURCE_VERSION | cut -c1-7)
  build:
    commands:
      - docker build -f $DOCKERFILE -t $REGISTRY/$IMAGE_REPO:$TAG -t $REGISTRY/$IMAGE_REPO:latest .
  post_build:
    commands:
      - docker push --all-tags $REGISTRY/$IMAGE_REPO
      - echo "pushed $REGISTRY/$IMAGE_REPO:$TAG"
EOF
python - "$TMPD" "$ACCOUNT_ID" "$AWS_REGION" "$GIT_URL" "$P" <<'PY'
import json, sys
tmpd, acct, region, url, p = sys.argv[1:]
spec = open(f"{tmpd}/buildspec.yml").read()
proj = {
  "name": f"{p}-build",
  "source": {"type": "GITHUB", "location": url, "buildspec": spec, "gitCloneDepth": 1},
  "sourceVersion": "main",
  "artifacts": {"type": "NO_ARTIFACTS"},
  "environment": {"type": "LINUX_CONTAINER", "image": "aws/codebuild/amazonlinux2-x86_64-standard:5.0",
                  "computeType": "BUILD_GENERAL1_SMALL", "privilegedMode": True,
                  "environmentVariables": [
                      {"name": "REGISTRY", "value": f"{acct}.dkr.ecr.{region}.amazonaws.com"},
                      {"name": "IMAGE_REPO", "value": f"{p}-booking"},
                      {"name": "DOCKERFILE", "value": "booking/Dockerfile"}]},
  "serviceRole": f"arn:aws:iam::{acct}:role/{p}-codebuild-role",
  "timeoutInMinutes": 20,
  "logsConfig": {"cloudWatchLogs": {"status": "ENABLED", "groupName": f"/{p}/codebuild"}},
}
json.dump(proj, open(f"{tmpd}/codebuild.json", "w"))
PY
if aws codebuild batch-get-projects --names "$P-build" --query 'projects[0].name' --output text | grep -q "$P-build"; then
  aws codebuild update-project --cli-input-json "file://$TMPD/codebuild.json" >/dev/null
else
  aws codebuild create-project --cli-input-json "file://$TMPD/codebuild.json" >/dev/null
fi

log "Start build"
BUILD_ID=$(aws codebuild start-build --project-name "$P-build" --query build.id --output text)
echo "$BUILD_ID" | tee "$TMPD/last_build_id"
