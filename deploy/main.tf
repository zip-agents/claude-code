# This service as an ECS service on the shared platform (the zip-platform repository).
# It sits in the private subnets, registered in Cloud Map as claude.zip.internal, and
# only the gateway can reach it (the platform's services security group). Everything
# shared is read from the platform's SSM parameters.

variable "region" {
  type = string
}

variable "image_tag" {
  description = "Git commit of the image to run (pushed to ECR by the deploy workflow)."
  type        = string
}

data "aws_caller_identity" "current" {}

data "aws_ssm_parameter" "platform" {
  for_each = toset(["cluster_arn", "private_subnet_ids", "services_security_group_id", "namespace_id"])
  name     = "/zip-platform/prod/${each.key}"
}

locals {
  name       = "claude"
  platform   = { for key, parameter in data.aws_ssm_parameter.platform : key => parameter.value }
  repository = "zip-platform/${local.name}"
  registry   = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com"
}

module "service" {
  # Pinned to a release of the platform repository; bump deliberately.
  source = "git::https://github.com/zip-agents/api-gateway.git//terraform/modules/ecs-service?ref=v1.0.0"

  name               = local.name
  image              = "${local.registry}/${local.repository}:${var.image_tag}"
  ecr_repository_arn = "arn:aws:ecr:${var.region}:${data.aws_caller_identity.current.account_id}:repository/${local.repository}"

  # Fargate Spot: ~70% cheaper. A reclaimed task gets SIGTERM and 120 s to drain (the
  # service's graceful shutdown), then ECS starts a replacement.
  capacity = "spot"
  cpu      = 256
  # 1 GiB covers the memory budget: MAX_CONCURRENT_REQUESTS (32) x OUTPUT_BUFFER_MAX_BYTES
  # (16 MiB) = 512 MiB of output buffers at most, plus the interpreter (README).
  memory        = 1024
  desired_count = 1

  cluster_arn            = local.platform["cluster_arn"]
  subnet_ids             = split(",", local.platform["private_subnet_ids"])
  security_group_ids     = [local.platform["services_security_group_id"]]
  cloud_map_namespace_id = local.platform["namespace_id"]

  environment_variables = {
    PORT      = "8000"
    LOG_LEVEL = "INFO"
    # Within the module's 120 s stopTimeout.
    SHUTDOWN_GRACE_SECONDS = "100"
  }
}

output "service" {
  value = module.service.service_name
}
