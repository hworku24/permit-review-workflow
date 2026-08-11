#!/usr/bin/env bash
# Build, push, and roll the service onto the new image.
#
# Separate from setup.sh because this is the one that runs often. It does not touch the
# database, the load balancer, or CloudFront.
set -euo pipefail
cd "$(dirname "$0")/../.."
source deploy/aws/config.sh

TAG="${1:-$(git rev-parse --short HEAD)}"

say "building ${TAG} for linux/amd64"
docker build --platform linux/amd64 -q -t "${ECR_REPO}:${TAG}" -t "${ECR_REPO}:latest" .

say "pushing"
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com" >/dev/null
docker push -q "${ECR_REPO}:${TAG}" >/dev/null
docker push -q "${ECR_REPO}:latest" >/dev/null

say "rolling the service"
aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" \
  --force-new-deployment --region "$AWS_REGION" --query 'service.serviceName' --output text
aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE" --region "$AWS_REGION"

say "done, ${TAG} is serving"
