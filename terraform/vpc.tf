# ---------------------------------------------------------------------------
# Phase 3 (part 1): VPC across >=2 AZs, public subnets for ALB/NAT only,
# private subnets for ECS tasks and RDS.
# ---------------------------------------------------------------------------

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${local.name_prefix}-vpc" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${local.name_prefix}-igw" }
}

resource "aws_subnet" "public" {
  for_each                = { for idx, az in var.availability_zones : az => idx }
  vpc_id                  = aws_vpc.this.id
  availability_zone       = each.key
  cidr_block              = cidrsubnet(var.vpc_cidr, 4, each.value)
  map_public_ip_on_launch = true
  tags                    = { Name = "${local.name_prefix}-public-${each.key}" }
}

resource "aws_subnet" "private" {
  for_each          = { for idx, az in var.availability_zones : az => idx }
  vpc_id            = aws_vpc.this.id
  availability_zone = each.key
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, each.value + 8)
  tags              = { Name = "${local.name_prefix}-private-${each.key}" }
}

resource "aws_eip" "nat" {
  for_each = aws_subnet.public
  domain   = "vpc"
  tags     = { Name = "${local.name_prefix}-nat-eip-${each.key}" }
}

# One NAT gateway per AZ -- so private-subnet egress (Secrets Manager,
# CloudWatch, Bedrock, ECR) survives a single-AZ outage, matching the plan's
# ">=2 AZ" requirement rather than sharing one NAT across both.
resource "aws_nat_gateway" "this" {
  for_each      = aws_subnet.public
  allocation_id = aws_eip.nat[each.key].id
  subnet_id     = each.value.id
  tags          = { Name = "${local.name_prefix}-nat-${each.key}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${local.name_prefix}-public-rt" }
}

resource "aws_route_table_association" "public" {
  for_each       = aws_subnet.public
  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  for_each = aws_subnet.private
  vpc_id   = aws_vpc.this.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this[each.key].id
  }
  tags = { Name = "${local.name_prefix}-private-rt-${each.key}" }
}

resource "aws_route_table_association" "private" {
  for_each       = aws_subnet.private
  subnet_id      = each.value.id
  route_table_id = aws_route_table.private[each.key].id
}

# ---------------------------------------------------------------------------
# Security groups -- ALB -> API only; API/worker -> RDS only on 5432;
# worker/API -> internet (via NAT) for Secrets Manager / CloudWatch / Bedrock.
# ---------------------------------------------------------------------------

resource "aws_security_group" "alb" {
  name_prefix = "${local.name_prefix}-alb-"
  vpc_id      = aws_vpc.this.id

  # Port 80 is either the temporary test endpoint or the HTTPS redirect.
  # Keep it restricted to the operator's client CIDR in both modes.
  ingress {
    description = "HTTP or HTTPS redirect from the configured client CIDR"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [var.allowed_client_cidr]
  }

  # 443 is only opened when an HTTPS listener is configured.
  dynamic "ingress" {
    for_each = var.acm_certificate_arn != null ? [1] : []
    content {
      description = "HTTPS from the configured client CIDR"
      from_port   = 443
      to_port     = 443
      protocol    = "tcp"
      cidr_blocks = [var.allowed_client_cidr]
    }
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name_prefix}-alb-sg" }
}

resource "aws_security_group" "ecs_api" {
  name_prefix = "${local.name_prefix}-ecs-api-"
  vpc_id      = aws_vpc.this.id

  ingress {
    description     = "ALB to API container"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name_prefix}-ecs-api-sg" }
}

resource "aws_security_group" "ecs_worker" {
  name_prefix = "${local.name_prefix}-ecs-worker-"
  vpc_id      = aws_vpc.this.id

  # No inbound rules at all -- the worker has no public inbound access and
  # no listener, matching Phase 5's requirement.
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name_prefix}-ecs-worker-sg" }
}

resource "aws_security_group" "rds" {
  name_prefix = "${local.name_prefix}-rds-"
  vpc_id      = aws_vpc.this.id

  ingress {
    description     = "API to PostgreSQL"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_api.id]
  }

  ingress {
    description     = "Worker to PostgreSQL"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_worker.id]
  }

  tags = { Name = "${local.name_prefix}-rds-sg" }
}
