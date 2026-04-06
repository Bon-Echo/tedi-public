terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket  = "bonecho-terraform-state"
    key     = "tedi-public/terraform.tfstate"
    region  = "us-west-2"
    encrypt = true
  }
}

provider "aws" {
  region = "us-east-1"
}

# ---------------------------------------------------------------------------
# Data Sources — discover shared infrastructure, never hardcode IDs
# ---------------------------------------------------------------------------

data "aws_vpc" "main" {
  filter {
    name   = "tag:Name"
    values = ["compactor-vpc"]
  }
}

data "aws_subnets" "public" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.main.id]
  }

  filter {
    name   = "tag:Name"
    values = ["compactor-subnet-*"]
  }

  filter {
    name   = "map-public-ip-on-launch"
    values = ["true"]
  }
}

data "aws_subnets" "private" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.main.id]
  }

  filter {
    name   = "tag:Name"
    values = ["compactor-subnet-*"]
  }

  filter {
    name   = "map-public-ip-on-launch"
    values = ["false"]
  }
}

data "aws_route53_zone" "main" {
  name = "bonecho.ai"
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}
