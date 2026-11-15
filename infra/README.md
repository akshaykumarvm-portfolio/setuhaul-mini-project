# Deployment notes (filled in during phases 3-4)

| Piece | Target | Notes |
| --- | --- | --- |
| Booking service | ECS on EC2, 1 container instance (t3.small, x86_64) | Service `desiredCount=1`, deployment `minimumHealthyPercent=0`, `maximumPercent=100` so two writers never overlap during a deploy. `/data` bind-mounted from the instance's EBS volume; nightly `sqlite3 .backup` to S3. ALB with HTTPS in front. |
| Agent | Bedrock AgentCore Runtime (arm64 container) | Built with AWS CodeBuild via the AgentCore starter toolkit, so no local Docker is required. JWT inbound auth against the Cognito pool. |
| Redis | Upstash (existing) | `rediss://` TLS. Plain keys only: Upstash has no RediSearch, so the LangGraph checkpointer is a small custom saver on GET/SET. |
| Web | S3 + CloudFront | `npm run build` locally or in CodeBuild. |
| Identity | Cognito user pool | Groups `driver`, `coordinator`, `planner`; custom attribute `driver_id`. |
| Secrets | AWS Secrets Manager | OPEN_ROUTER_API_KEY, LANGSMITH_API_KEY, REDIS_URL. |
