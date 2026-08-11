#!/usr/bin/env bash
# Remove everything that bills. Run this when you are not interviewing.
#
# Leaves the ECR repository and the secrets, which cost cents and save re-entering
# passphrases. Pass --all to remove those too.
set -uo pipefail
cd "$(dirname "$0")/../.."
source deploy/aws/config.sh

say "CloudFront (disable, then delete once it has propagated)"
DIST=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Comment=='PermitFlow demonstration deployment. HTTPS front door for the ALB.'].Id | [0]" \
  --output text 2>/dev/null)
if [ "$DIST" != "None" ] && [ -n "$DIST" ]; then
  ETAG=$(aws cloudfront get-distribution-config --id "$DIST" --query ETag --output text)
  aws cloudfront get-distribution-config --id "$DIST" --query DistributionConfig > /tmp/cf-off.json
  python3 - <<'PY'
import json
c = json.load(open("/tmp/cf-off.json")); c["Enabled"] = False
json.dump(c, open("/tmp/cf-off.json", "w"))
PY
  aws cloudfront update-distribution --id "$DIST" --distribution-config file:///tmp/cf-off.json --if-match "$ETAG" >/dev/null
  echo "  disabled ${DIST}. Deleting takes about 15 minutes; run:"
  echo "  aws cloudfront delete-distribution --id ${DIST} --if-match \$(aws cloudfront get-distribution --id ${DIST} --query ETag --output text)"
  rm -f /tmp/cf-off.json
fi

say "ECS service and cluster"
aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --desired-count 0 --region "$AWS_REGION" >/dev/null 2>&1
aws ecs delete-service --cluster "$CLUSTER" --service "$SERVICE" --force --region "$AWS_REGION" >/dev/null 2>&1 && echo "  service deleted"
aws ecs delete-cluster --cluster "$CLUSTER" --region "$AWS_REGION" >/dev/null 2>&1 && echo "  cluster deleted"

say "load balancer and target group"
ALB=$(aws elbv2 describe-load-balancers --names permitflow-alb --region "$AWS_REGION" --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null)
[ -n "$ALB" ] && [ "$ALB" != "None" ] && aws elbv2 delete-load-balancer --load-balancer-arn "$ALB" --region "$AWS_REGION" && echo "  ALB deleted"
sleep 20
TG=$(aws elbv2 describe-target-groups --names permitflow-tg --region "$AWS_REGION" --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null)
[ -n "$TG" ] && [ "$TG" != "None" ] && aws elbv2 delete-target-group --target-group-arn "$TG" --region "$AWS_REGION" && echo "  target group deleted"

say "RDS (no final snapshot: every row is generated and reproducible)"
aws rds delete-db-instance --db-instance-identifier "$DB_INSTANCE" --skip-final-snapshot \
  --delete-automated-backups --region "$AWS_REGION" --query 'DBInstance.DBInstanceStatus' --output text 2>/dev/null

say "what is left"
echo "  ECR images, Secrets Manager entries, IAM role, log group, security groups."
echo "  Together they are under a dollar a month. Pass --all to remove them too."

if [ "${1:-}" = "--all" ]; then
  say "removing the rest"
  aws ecr delete-repository --repository-name "$PROJECT" --force --region "$AWS_REGION" >/dev/null 2>&1 && echo "  ECR gone"
  for s in db-url db-password demo-passphrase session-secret; do
    aws secretsmanager delete-secret --secret-id "permitflow/$s" --force-delete-without-recovery --region "$AWS_REGION" >/dev/null 2>&1 && echo "  secret $s gone"
  done
  aws logs delete-log-group --log-group-name "$LOG_GROUP" --region "$AWS_REGION" >/dev/null 2>&1 && echo "  logs gone"
fi

say "teardown finished"
