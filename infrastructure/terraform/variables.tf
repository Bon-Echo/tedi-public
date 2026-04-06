variable "service_name" {
  description = "Name of the service"
  type        = string
  default     = "tedi-public"
}

variable "environment" {
  description = "Deployment environment"
  type        = string
  default     = "production"
}

variable "instance_type" {
  description = "EC2 instance type"
  type        = string
  default     = "t3.small"
}

variable "key_pair_name" {
  description = "Name of the AWS key pair for EC2 instances"
  type        = string
  default     = "compactor-key"
}

# ---------------------------------------------------------------------------
# Domain — pending Founder decision
# Default: tedi-public.bonecho.ai (subdomain)
# Alternative: tedi.bonecho.ai (if Founder confirms subdirectory not feasible
#              at infra level, or use bonecho.ai/tedi via bonecho-web)
# ---------------------------------------------------------------------------

variable "domain" {
  description = "Public domain name for the service (update after Founder decision)"
  type        = string
  default     = "tedi-public.bonecho.ai"
}

variable "certbot_email" {
  description = "Email address for Let's Encrypt certificate registration"
  type        = string
  default     = "admin@bonecho.ai"
}

# ---------------------------------------------------------------------------
# RDS PostgreSQL
# ---------------------------------------------------------------------------

variable "db_instance_class" {
  description = "RDS instance class"
  type        = string
  default     = "db.t3.micro"
}

variable "db_name" {
  description = "Database name"
  type        = string
  default     = "tedi_public"
}

variable "db_username" {
  description = "Database master username"
  type        = string
  default     = "tedi_public"
}

variable "db_password" {
  description = "Database master password"
  type        = string
  sensitive   = true
}

# ---------------------------------------------------------------------------
# Sensitive variables — passed via TF_VAR_ in CI/CD
# ---------------------------------------------------------------------------

variable "anthropic_api_key" {
  description = "API key for Anthropic Claude"
  type        = string
  sensitive   = true
}

variable "elevenlabs_api_key" {
  description = "API key for ElevenLabs TTS"
  type        = string
  sensitive   = true
}

variable "elevenlabs_voice_id" {
  description = "ElevenLabs voice ID"
  type        = string
  sensitive   = true
}

variable "deepgram_api_key" {
  description = "API key for Deepgram STT"
  type        = string
  sensitive   = true
}

variable "slack_webhook_url" {
  description = "Slack incoming webhook URL for #board-room notifications"
  type        = string
  sensitive   = true
  default     = ""
}
