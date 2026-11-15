# AgentCore Runtime container: linux/arm64, serves /invocations and /ping on 8080.
FROM --platform=linux/arm64 python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY agent/requirements.txt agent/requirements.txt
RUN pip install --no-cache-dir -r agent/requirements.txt
COPY agent/ agent/
EXPOSE 8080
CMD ["python", "-m", "agent.app"]
