#!/usr/bin/env bash
# Build the React app for Cognito sign-in and publish it to the web bucket behind CloudFront.
set -euo pipefail
source "$(dirname "$0")/env.sh"
source "$TMPD/cognito.env"
source "$TMPD/web.env"
export PATH="/c/Program Files/nodejs:$PATH"   # Windows Node install (no-op elsewhere)

log "Build web (Cognito mode)"
cd "$REPO_ROOT/web"
[ -d node_modules ] || npm ci --no-audit --no-fund
VITE_AUTH_MODE=cognito VITE_API_BASE=/api VITE_COGNITO_USER_POOL_ID="$COGNITO_USER_POOL_ID" \
  VITE_COGNITO_CLIENT_ID="$COGNITO_CLIENT_ID" npm run build

log "Upload to s3://$WEB_BUCKET (hashed assets cached long, index.html never)"
aws s3 sync dist/assets "s3://$WEB_BUCKET/assets" --delete --cache-control "public,max-age=31536000,immutable" --only-show-errors
aws s3 cp dist/index.html "s3://$WEB_BUCKET/index.html" --cache-control "no-cache" --only-show-errors
aws s3 cp dist/favicon.svg "s3://$WEB_BUCKET/favicon.svg" --only-show-errors
aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/index.html" "/" >/dev/null
echo "https://$WEB_DOMAIN"
