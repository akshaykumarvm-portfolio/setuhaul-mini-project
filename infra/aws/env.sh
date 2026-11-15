# Shared settings for the AWS scripts. Source this file; don't run it.
export MSYS_NO_PATHCONV=1              # Git Bash on Windows: keep "/setuhaul/..." names intact
export AWS_PAGER=""
export AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="$AWS_REGION"
export ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
export P=setuhaul
export BUCKET="$P-data-$ACCOUNT_ID"
export SNAPSHOT_KEY="snapshot/2026-08-04/setuhaul_schema_and_seed.sql"
export DB_ID="$P-db"
export CLUSTER="$P"
export SERVICE="$P-booking"
export ECR_REPO="$P-booking"
export LOG_GROUP="/$P/booking"
export GIT_URL="https://github.com/akshaykumarvm-portfolio/setuhaul-mini-project.git"
export REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# The Windows AWS CLI needs C:/... paths for file:// arguments
command -v cygpath >/dev/null && REPO_ROOT="$(cygpath -m "$REPO_ROOT")"
export TMPD="${TMPD:-$REPO_ROOT/var/aws}"
mkdir -p "$TMPD"

log() { printf '\n== %s\n' "$*"; }
