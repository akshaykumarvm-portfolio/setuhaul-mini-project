# AWS deployment (us-east-1, free tier + AgentCore, $20/month budget)

| Piece | Service | Free-tier sizing / notes |
| --- | --- | --- |
| System of record | RDS for PostgreSQL 16 | `db.t4g.micro`, 20 GB gp3, single-AZ, no public access; SG allows 5432 only from the ECS instance SG. DB created with `LC_COLLATE 'C'` by `booking.db.migrate`. |
| Supplied data | S3 (private bucket) | `s3://setuhaul-data-<account>/snapshot/2026-08-04/`; block public access, versioning on. The booking task reads the seed SQL into memory via its task role (`s3:GetObject` on that prefix only). |
| Booking service | ECS on EC2 | 1 × `t3.micro` container instance; service `desiredCount=1`, `minimumHealthyPercent=0`, `maximumPercent=100`. **No volumes**: the container is stateless. |
| Container images | ECR | Booking + agent repos. Pushed from CodeBuild (no local Docker). |
| Agent | Bedrock AgentCore Runtime | arm64 container; JWT inbound auth against Cognito. Pay-per-use (not free tier), covered by the budget. |
| Redis | Upstash (existing) | `rediss://` TLS. Plain keys only (no RediSearch): custom LangGraph checkpointer. |
| Web | S3 + CloudFront | Static Vite build. |
| Identity | Cognito user pool | Groups `driver`, `coordinator`, `planner`; custom attribute `driver_id`. |
| Secrets | SSM Parameter Store (SecureString) | Free tier, instead of Secrets Manager. `DATABASE_URL`, `OPEN_ROUTER_API_KEY`, `LANGSMITH_API_KEY`, `REDIS_URL`. |
| Cost guard | AWS Budgets | `setuhaul-20usd` **created**: $20/month, counts usage before credits; email at 50/80/100% actual and 100% forecast. |

## Status update (2026-11-15, later)

The account was upgraded to the **Paid plan** (`get-account-plan-state` now returns PAID / ACTIVE).
The CLI session issued before the upgrade is still denied by the same policies; a fresh
`aws login` is needed before provisioning can start.

## Original blocker (2026-11-15)

Account `611597330436` (Free plan, $99.98 credits) is a member of AWS Organization
`o-mvhgz7rhpw` (management account `688508115450`). Its service control policies
explicitly deny, among others: `s3:CreateBucket`, `ec2:*Describe*`, `rds:*`, `ecs:*`,
`ecr:*`, `cognito-idp:*`, `bedrock-agentcore:*`, `ssm:*`, `secretsmanager:*`,
`codebuild:*`, `lambda:*`, `iam:ListAccountAliases`. Allowed so far: Budgets,
CloudFront (read), CloudWatch Logs (read), IAM (read), Lightsail (read), S3 (list).

The CLI session was a role (`AccountFullAccessRole`) rather than the account's root or
an IAM user, which is why these organization policies apply. Nothing beyond the budget
has been created.
