# ---------------------------------------------------------------------------
# DNS A Record — tedi-public.bonecho.ai → Instance Public IP
#
# NOTE: Domain is pending Founder decision (bonecho.ai/tedi vs tedi-public.bonecho.ai).
# If the decision is bonecho.ai/tedi (subdirectory), this record should be removed
# and routing handled in the bonecho-web nginx/CDN config instead.
# Change var.domain to switch the subdomain.
#
# Using instance public_ip directly (no EIP — AWS account has reached EIP limit).
# ---------------------------------------------------------------------------

resource "aws_route53_record" "main" {
  zone_id = data.aws_route53_zone.main.zone_id
  name    = replace(var.domain, ".bonecho.ai", "")
  type    = "A"
  ttl     = 60
  records = [aws_instance.main.public_ip]
}
