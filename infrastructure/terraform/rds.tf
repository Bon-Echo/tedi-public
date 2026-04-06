# ---------------------------------------------------------------------------
# RDS PostgreSQL — tedi-public user/session database
# Separate from existing tedi infrastructure.
# ---------------------------------------------------------------------------

# DB subnet group — spans multiple AZs for HA (uses private subnets if available,
# falls back to public subnets if the VPC only has public subnets)
resource "aws_db_subnet_group" "main" {
  name        = "${local.name_prefix}-db-subnet-group"
  description = "Subnet group for ${var.service_name} RDS instance"
  subnet_ids  = length(data.aws_subnets.private.ids) > 0 ? tolist(data.aws_subnets.private.ids) : tolist(data.aws_subnets.public.ids)

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-db-subnet-group"
  })
}

resource "aws_db_instance" "main" {
  identifier        = "${local.name_prefix}-db"
  engine            = "postgres"
  engine_version    = "16.3"
  instance_class    = var.db_instance_class
  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = var.db_name
  username = var.db_username
  password = var.db_password

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]

  # Backups
  backup_retention_period = 7
  backup_window           = "03:00-04:00"
  maintenance_window      = "mon:04:00-mon:05:00"

  # No multi-AZ for cost savings at launch; enable for production scale
  multi_az = false

  # Delete protection — must be disabled manually before destroy
  deletion_protection = true

  skip_final_snapshot       = false
  final_snapshot_identifier = "${local.name_prefix}-db-final-snapshot"

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-db"
  })
}
