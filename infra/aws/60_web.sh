#!/usr/bin/env bash
# Private S3 bucket for the React build + one CloudFront distribution:
#   /*      -> S3 (Origin Access Control, cached)
#   /api/*  -> booking service on EC2 :8000 (no cache, all headers incl. Authorization, SSE streams)
# Safe to re-run: creates once, otherwise only refreshes the API origin's hostname.
set -euo pipefail
source "$(dirname "$0")/env.sh"
WEB_BUCKET="$P-web-$ACCOUNT_ID"

log "Web bucket $WEB_BUCKET (private)"
aws s3api head-bucket --bucket "$WEB_BUCKET" 2>/dev/null || aws s3api create-bucket --bucket "$WEB_BUCKET" >/dev/null
aws s3api put-public-access-block --bucket "$WEB_BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

log "Booking origin: EC2 public DNS, port 8000 open to CloudFront's origin-facing range"
IID=$(aws ec2 describe-instances --filters Name=tag:Name,Values="$P-ecs" Name=instance-state-name,Values=running \
      --query 'Reservations[0].Instances[0].InstanceId' --output text)
EC2_DNS=$(aws ec2 describe-instances --instance-ids "$IID" --query 'Reservations[0].Instances[0].PublicDnsName' --output text)
SG=$(aws ec2 describe-security-groups --filters Name=group-name,Values="$P-booking-sg" --query 'SecurityGroups[0].GroupId' --output text)
PL=$(aws ec2 describe-managed-prefix-lists --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing \
     --query 'PrefixLists[0].PrefixListId' --output text)
aws ec2 authorize-security-group-ingress --group-id "$SG" \
  --ip-permissions "IpProtocol=tcp,FromPort=8000,ToPort=8000,PrefixListIds=[{PrefixListId=$PL,Description=CloudFront}]" >/dev/null 2>&1 || true
echo "origin $EC2_DNS:8000 (sg $SG allows $PL)"

log "Origin Access Control"
OAC=$(aws cloudfront list-origin-access-controls --query "OriginAccessControlList.Items[?Name=='$P-web-oac'].Id | [0]" --output text)
if [ "$OAC" = "None" ]; then
  OAC=$(aws cloudfront create-origin-access-control --origin-access-control-config \
    "Name=$P-web-oac,SigningProtocol=sigv4,SigningBehavior=always,OriginAccessControlOriginType=s3" \
    --query OriginAccessControl.Id --output text)
fi

DIST=$(aws cloudfront list-distributions --query "DistributionList.Items[?Comment=='$P'].Id | [0]" --output text)
if [ "$DIST" = "None" ] || [ -z "$DIST" ]; then
  log "Create distribution (takes ~5 minutes to deploy)"
  python - "$TMPD/dist.json" "$WEB_BUCKET" "$AWS_REGION" "$OAC" "$EC2_DNS" "$P" <<'PY'
import json, sys, time
out, bucket, region, oac, ec2, p = sys.argv[1:]
CACHING_OPTIMIZED = "658327ea-f89d-4fab-a63d-7e88639e58f6"
CACHING_DISABLED = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
ALL_VIEWER_EXCEPT_HOST = "b689b0a8-53d0-40ab-baf2-68738e2966ac"
cfg = {
  "CallerReference": f"{p}-{int(time.time())}", "Comment": p, "Enabled": True, "PriceClass": "PriceClass_100",
  "DefaultRootObject": "index.html", "HttpVersion": "http2and3", "IsIPV6Enabled": True,
  "Origins": {"Quantity": 2, "Items": [
    {"Id": "web", "DomainName": f"{bucket}.s3.{region}.amazonaws.com", "OriginAccessControlId": oac,
     "S3OriginConfig": {"OriginAccessIdentity": ""}},
    {"Id": "api", "DomainName": ec2, "CustomOriginConfig": {
        "HTTPPort": 8000, "HTTPSPort": 443, "OriginProtocolPolicy": "http-only",
        "OriginReadTimeout": 60, "OriginKeepaliveTimeout": 60,
        "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]}}}]},
  "DefaultCacheBehavior": {
    "TargetOriginId": "web", "ViewerProtocolPolicy": "redirect-to-https", "Compress": True,
    "CachePolicyId": CACHING_OPTIMIZED,
    "AllowedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"], "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}}},
  "CacheBehaviors": {"Quantity": 1, "Items": [{
    "PathPattern": "/api/*", "TargetOriginId": "api", "ViewerProtocolPolicy": "https-only", "Compress": False,
    "CachePolicyId": CACHING_DISABLED, "OriginRequestPolicyId": ALL_VIEWER_EXCEPT_HOST,
    "AllowedMethods": {"Quantity": 7, "Items": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
                       "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}}}]},
}
json.dump(cfg, open(out, "w"))
PY
  DIST=$(aws cloudfront create-distribution --distribution-config "file://$TMPD/dist.json" --query Distribution.Id --output text)
else
  log "Distribution $DIST exists: point the API origin at $EC2_DNS"
  aws cloudfront get-distribution-config --id "$DIST" > "$TMPD/dist_current.json"
  python - "$TMPD/dist_current.json" "$TMPD/dist_update.json" "$EC2_DNS" <<'PY'
import json, sys
src, out, ec2 = sys.argv[1:]
d = json.load(open(src))
cfg = d["DistributionConfig"]
for o in cfg["Origins"]["Items"]:
    if o["Id"] == "api":
        o["DomainName"] = ec2
json.dump(cfg, open(out, "w"))
open(out + ".etag", "w").write(d["ETag"])
PY
  aws cloudfront update-distribution --id "$DIST" --if-match "$(cat "$TMPD/dist_update.json.etag")" \
    --distribution-config "file://$TMPD/dist_update.json" >/dev/null
fi
DOMAIN=$(aws cloudfront get-distribution --id "$DIST" --query Distribution.DomainName --output text)

log "Bucket policy: only this distribution may read"
cat > "$TMPD/web-bucket-policy.json" <<EOF
{"Version":"2012-10-17","Statement":[{"Sid":"CloudFrontRead","Effect":"Allow",
 "Principal":{"Service":"cloudfront.amazonaws.com"},"Action":"s3:GetObject","Resource":"arn:aws:s3:::$WEB_BUCKET/*",
 "Condition":{"StringEquals":{"AWS:SourceArn":"arn:aws:cloudfront::$ACCOUNT_ID:distribution/$DIST"}}}]}
EOF
aws s3api put-bucket-policy --bucket "$WEB_BUCKET" --policy "file://$TMPD/web-bucket-policy.json"

printf 'DIST_ID=%s\nWEB_DOMAIN=%s\nWEB_BUCKET=%s\n' "$DIST" "$DOMAIN" "$WEB_BUCKET" > "$TMPD/web.env"
echo "https://$DOMAIN  (distribution $DIST)"
