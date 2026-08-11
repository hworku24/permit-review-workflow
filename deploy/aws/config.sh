# Everything the deploy scripts need to know. Sourced by the others.
#
# The VPC and subnets are the account's default ones. A deployment for real permit data
# would build its own with private subnets and a NAT gateway; deploy/aws/README.md says why
# this one does not.

export AWS_PAGER=""
export AWS_REGION="${AWS_REGION:-us-east-1}"
export ACCOUNT_ID="${ACCOUNT_ID:-559037158320}"

export PROJECT=permitflow
export ECR_REPO="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${PROJECT}"
export CLUSTER=permitflow
export SERVICE=permitflow
export LOG_GROUP=/ecs/permitflow

export VPC_ID="${VPC_ID:-vpc-079faf08b34e5152d}"
export SUBNETS="${SUBNETS:-subnet-056987c56ac623103,subnet-0bb291cb996589755,subnet-09ea502ae7da191cc}"

export DB_INSTANCE=permitflow-db
export DB_CLASS=db.t4g.micro
export DB_STORAGE=20

# How many cases the deployed database is seeded with.
export DEMO_CASES="${DEMO_CASES:-120}"

say() { printf '\n== %s\n' "$1"; }
