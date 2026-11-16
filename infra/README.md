# AWS deployment (account 688508115450, us-east-1, $20/month budget)

## What is live (phase 4)

| Piece | Resource | Notes |
| --- | --- | --- |
| System of record | RDS PostgreSQL 16.15 `setuhaul-db` | `db.t4g.micro`, 20 GB gp3, encrypted, **not publicly accessible**; SG allows 5432 only from the booking SG. Database `setuhaul` (C collation) created by the service on first start. |
| Supplied data | S3 `setuhaul-data-688508115450/snapshot/2026-08-04/` | Private (public access blocked), versioned, SSE-S3. The booking task reads the seed SQL into memory via its task role (`s3:GetObject` on `snapshot/*` only). Nothing stored on EC2. |
| Booking service | ECS cluster `setuhaul`, service `setuhaul-booking` on 1 × EC2 `t3.micro` | `desiredCount=1`, `minimumHealthyPercent=0`, `maximumPercent=100` (never two writers). Stateless task, no volumes. Port 8000 open only to CloudFront and the owner's IP; every request needs a Cognito token or the agent key. |
| Images | ECR `setuhaul-booking` (keeps 5 newest) | Built by CodeBuild `setuhaul-build` from the public GitHub repo; no local Docker. |
| Secrets | SSM Parameter Store `/setuhaul/*` (SecureString) | `DATABASE_URL`, `DB_PASSWORD`, `REDIS_URL`, `OPEN_ROUTER_API_KEY`, `LANGSMITH_API_KEY`. |
| Logs | CloudWatch `/setuhaul/booking`, `/setuhaul/codebuild` | 7-day retention on the service log. |
| Cost guard | Budget `setuhaul-20usd` | $20/month gross usage (credits not netted off); emails at 50/80/100% actual and 100% forecast. |
| Sign-in | Cognito user pool `setuhaul-users` (Lite tier) | Groups `driver`/`coordinator`/`planner`; attributes `custom:driver_id`, `custom:facility_id`; web client without secret; 11 demo users (`40_cognito.sh`). |
| Web + API edge | CloudFront `setuhaul` + private S3 `setuhaul-web-<account>` | `/*` from S3 via Origin Access Control; `/api/*` to EC2 :8000 uncached with all viewer headers (Authorization, SSE). EC2 port 8000 accepts CloudFront's managed prefix list + the owner's IP. |
| Agent | Bedrock AgentCore Runtime `setuhaul_agent` | ARM64 image from CodeBuild `setuhaul-agent-build` (ECR `setuhaul-agent`, pinned by digest); Cognito JWT inbound auth (audience = web client); secrets from SSM at start-up; calls the booking API through CloudFront with `X-Agent-Key`. |

## Scripts (Git Bash; run from the repo root)

| Script | Does |
| --- | --- |
| `bash infra/aws/10_foundation.sh` | Budget (needs `BUDGET_EMAIL`), S3 bucket + upload, security groups, RDS |
| `bash infra/aws/20_build.sh` | SSM secrets from `.env`, ECR, CodeBuild project, starts an image build |
| `bash infra/aws/30_ecs.sh` | IAM roles, cluster, EC2 container instance, task definition, service (redeploys if it exists) |
| `bash infra/aws/40_cognito.sh` | User pool, groups, web client, demo users, demo password + agent key in SSM |
| `bash infra/aws/60_web.sh` | Web bucket, CloudFront distribution (or re-point its API origin) |
| `bash infra/aws/50_agentcore.sh` | Agent image (ARM64), execution role, AgentCore runtime (create or update) |
| `bash infra/aws/70_web_deploy.sh` | Build the React app for Cognito and publish it |
| `bash infra/aws/stop.sh` | **Pause**: service to 0, stop EC2 and RDS. Storage only while stopped (~$3/month) |
| `bash infra/aws/start.sh` | Resume; prints the new endpoint (public DNS changes after a stop) |

First-time order: 10 → 20 → 30 → 40 → 60 → 50 → 30 (again, now with Cognito + AgentCore) → 70.
Redeploy after a code push: booking `20_build.sh` then `30_ecs.sh`; agent `50_agentcore.sh`; web `70_web_deploy.sh`.
If your IP changes: `aws ec2 authorize-security-group-ingress --group-name setuhaul-booking-sg --protocol tcp --port 8000 --cidr <ip>/32`.

## Rough cost (us-east-1, on-demand)

| Item | Running 24/7 | Stopped |
| --- | --- | --- |
| EC2 t3.micro + 30 GB gp3 | ~$10 | ~$2.40 (disk) |
| Public IPv4 | ~$3.65 | $0 |
| RDS db.t4g.micro + 20 GB | ~$14 | ~$2.30 (storage) |
| S3, ECR, CodeBuild, SSM, logs | < $1 | < $1 |
| CloudFront, Cognito (Lite, < 10k users) | free tier | free tier |
| AgentCore Runtime | per use: cents for a demo session | $0 |

## History

The first attempt used account 611597330436 (Free plan). It sits in this account's AWS Organization
(`o-mvhgz7rhpw`), whose service control policies block EC2, RDS, ECS, S3 bucket creation and more for
Free-plan member accounts. Only a budget (`setuhaul-20usd`) was created there; it costs nothing.
