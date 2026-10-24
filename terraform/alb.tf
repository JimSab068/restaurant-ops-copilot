# ---------------------------------------------------------------------------
# ALB in public subnets, forwarding to the API service in private subnets.
# Health check hits /readyz (DB-backed), not /healthz -- see api/main.py.
#
# Two modes, selected by var.acm_certificate_arn:
#   - SET (the only mode that belongs in staging/prod): HTTPS:443 forwards
#     to the target group; HTTP:80 exists only to redirect to HTTPS. The
#     security group (vpc.tf) opens both ports to match.
#   - UNSET (isolated initial test only): HTTP:80 forwards directly, no
#     443 listener exists, and the security group only opens 80 -- there
#     is no mismatch between "what's open" and "what's listening" in
#     either mode. Do not point a real domain or real tenant traffic at
#     this mode; it exists so `terraform apply` is plan-able on day one
#     without an ACM dependency blocking you.
# ---------------------------------------------------------------------------

resource "aws_lb" "api" {
  name               = "${local.name_prefix}-api-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = [for s in aws_subnet.public : s.id]
}

resource "aws_lb_target_group" "api" {
  name        = "${local.name_prefix}-api-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.this.id
  target_type = "ip" # required for awsvpc-networked Fargate tasks

  health_check {
    path                = "/readyz"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

# --- HTTPS mode (var.acm_certificate_arn set) ---

resource "aws_lb_listener" "api_https" {
  count             = var.acm_certificate_arn != null ? 1 : 0
  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.acm_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_lb_listener" "api_http_redirect" {
  count             = var.acm_certificate_arn != null ? 1 : 0
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# --- Isolated-test mode (var.acm_certificate_arn unset) ---

resource "aws_lb_listener" "api_http_direct" {
  count             = var.acm_certificate_arn == null ? 1 : 0
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
