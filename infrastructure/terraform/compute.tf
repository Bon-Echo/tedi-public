# ---------------------------------------------------------------------------
# Latest Amazon Linux 2023 AMI
# ---------------------------------------------------------------------------

data "aws_ami" "amazon_linux" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  filter {
    name   = "state"
    values = ["available"]
  }
}

# ---------------------------------------------------------------------------
# EC2 Instance
# ---------------------------------------------------------------------------

resource "aws_instance" "main" {
  ami                         = data.aws_ami.amazon_linux.id
  instance_type               = var.instance_type
  key_name                    = var.key_pair_name
  iam_instance_profile        = aws_iam_instance_profile.ec2.name
  vpc_security_group_ids      = [aws_security_group.ec2.id]
  subnet_id                   = tolist(data.aws_subnets.public.ids)[0]
  user_data_replace_on_change = true

  user_data = base64encode(templatefile("${path.module}/user_data.sh.tpl", {
    aws_region         = data.aws_region.current.name
    ecr_repository_url = aws_ecr_repository.main.repository_url
    env_file_content   = local.env_file_content
    service_name       = var.service_name
    certbot_email      = var.certbot_email
    domain             = var.domain
  }))

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-instance"
  })

  depends_on = [aws_db_instance.main]
}

# ---------------------------------------------------------------------------
# Elastic IP — stable public IP so DNS survives instance stop/start
# ---------------------------------------------------------------------------

resource "aws_eip" "main" {
  instance = aws_instance.main.id
  domain   = "vpc"

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-eip"
  })
}
