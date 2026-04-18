locals {
  name_prefix = "${var.service_name}-${var.environment}"

  common_tags = {
    Service     = var.service_name
    Environment = var.environment
    ManagedBy   = "terraform"
    Project     = "bonecho"
  }

  ecr_repository_url = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${data.aws_region.current.name}.amazonaws.com/${var.service_name}"

  env_file_content = join("\n", [
    "ENVIRONMENT=${var.environment}",
    "SERVICE_NAME=${var.service_name}",
    "APP_ENV=${var.environment}",
    "AWS_REGION=${data.aws_region.current.name}",
    "ANTHROPIC_API_KEY=${var.anthropic_api_key}",
    "ELEVENLABS_API_KEY=${var.elevenlabs_api_key}",
    "ELEVENLABS_VOICE_ID=${var.elevenlabs_voice_id}",
    "DEEPGRAM_API_KEY=${var.deepgram_api_key}",
    "SLACK_WEBHOOK_URL=${var.slack_webhook_url}",
    "ADMIN_SESSION_SECRET=${var.admin_session_secret}",
    "S3_BUCKET_NAME=${aws_s3_bucket.artifacts.id}",
    "DATABASE_URL=postgresql+asyncpg://${var.db_username}:${var.db_password}@${aws_db_instance.main.address}:5432/${var.db_name}",
    "SES_FROM_EMAIL=tedi@bonecho.ai",
    "FOLLOWUP_FROM_EMAIL=sifat@bonecho.ai",
    "SERVICE_URL=https://${var.domain}",
    "DAILY_SESSION_CAP=30",
  ])
}
