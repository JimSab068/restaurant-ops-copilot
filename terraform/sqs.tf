# ---------------------------------------------------------------------------
# Phase 4: SQS FIFO queue + DLQ, redrive policy, alarms.
#
# MessageGroupId=tenant_id / MessageDeduplicationId=work_request_id are set
# at the application layer (shared/queue.py) -- nothing to configure here
# for that, FIFO queues require it on every send_message call.
# ---------------------------------------------------------------------------

resource "aws_sqs_queue" "dlq" {
  name                      = "${local.name_prefix}-work-requests-dlq.fifo"
  fifo_queue                = true
  message_retention_seconds = 1209600 # 14 days -- max retention, so a stuck
                                       # DLQ message survives a long weekend
                                       # before anyone looks at the alarm.
}

resource "aws_sqs_queue" "work_requests" {
  name                       = "${local.name_prefix}-work-requests.fifo"
  fifo_queue                 = true
  content_based_deduplication = false # we set MessageDeduplicationId explicitly
  visibility_timeout_seconds = 120    # must exceed the worker's max expected
                                       # execute_tool_call_idempotent() time;
                                       # tune against observed p99 latency.
  message_retention_seconds  = 345600 # 4 days

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount      = 5
  })
}

resource "aws_sns_topic" "ops_alarms" {
  name = "${local.name_prefix}-ops-alarms"
}

resource "aws_cloudwatch_metric_alarm" "dlq_messages" {
  alarm_name          = "${local.name_prefix}-dlq-has-messages"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = [aws_sns_topic.ops_alarms.arn]
  treat_missing_data  = "notBreaching"
}

resource "aws_cloudwatch_metric_alarm" "queue_backlog_age" {
  alarm_name          = "${local.name_prefix}-queue-oldest-message-age"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = aws_sqs_queue.work_requests.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 600 # 10 minutes
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = [aws_sns_topic.ops_alarms.arn]
  treat_missing_data  = "notBreaching"
}

resource "aws_cloudwatch_metric_alarm" "queue_backlog_depth" {
  alarm_name          = "${local.name_prefix}-queue-backlog-depth"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.work_requests.name }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  threshold           = 500
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = [aws_sns_topic.ops_alarms.arn]
  treat_missing_data  = "notBreaching"
}

resource "aws_cloudwatch_metric_alarm" "worker_cpu_pegged" {
  alarm_name          = "${local.name_prefix}-worker-cpu-sustained-high"
  namespace           = "AWS/ECS"
  metric_name         = "CPUUtilization"
  dimensions          = { ClusterName = aws_ecs_cluster.this.name, ServiceName = aws_ecs_service.worker.name }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 90
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = [aws_sns_topic.ops_alarms.arn]
  treat_missing_data  = "notBreaching"
}
