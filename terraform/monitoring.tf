# Monitoring and alerts for the test service. Off until var.monitoring_enabled is true, because
# creating these needs roles/monitoring.editor and roles/logging.configWriter on the operator
# account (iam.tf), which only an IAM admin can grant. Costs: log-based metrics, alert policies
# and one uptime check every 5 minutes stay within Cloud Monitoring's free allotments at this
# volume (estimate: US$ 0/month).

locals {
  monitoring = var.monitoring_enabled ? 1 : 0
  # Logs of the test service and of its jobs.
  app_logs = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${local.service_name}\""
}

resource "google_monitoring_notification_channel" "email" {
  count = local.monitoring

  display_name = "LucroMEI alertas (e-mail)"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
}

resource "google_logging_metric" "job_dead" {
  count = local.monitoring

  name   = "lucromei/job_dead"
  filter = "${local.app_logs} AND jsonPayload.event=\"job_dead\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    labels {
      key        = "job_kind"
      value_type = "STRING"
    }
  }
  label_extractors = {
    job_kind = "EXTRACT(jsonPayload.job_kind)"
  }
}

resource "google_logging_metric" "delivery_failed" {
  count = local.monitoring

  name   = "lucromei/whatsapp_delivery_failed"
  filter = "${local.app_logs} AND jsonPayload.event=\"whatsapp_delivery_failed\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_logging_metric" "queue_lag" {
  count = local.monitoring

  name            = "lucromei/queue_oldest_due_seconds"
  filter          = "${local.app_logs} AND jsonPayload.event=\"queue_health\""
  value_extractor = "EXTRACT(jsonPayload.oldest_due_seconds)"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "s"
  }
  bucket_options {
    exponential_buckets {
      num_finite_buckets = 16
      growth_factor      = 2
      scale              = 1
    }
  }
}

resource "google_monitoring_uptime_check_config" "health" {
  count = local.monitoring

  display_name = "lucromei-test /health"
  timeout      = "10s"
  period       = "300s"

  http_check {
    path         = "/health"
    port         = 443
    use_ssl      = true
    validate_ssl = true
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = local.project_id
      host       = trimprefix(google_cloud_run_v2_service.test.uri, "https://")
    }
  }
}

resource "google_monitoring_alert_policy" "uptime" {
  count = local.monitoring

  display_name          = "LucroMEI teste fora do ar"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.email[0].id]

  conditions {
    display_name = "/health falhou em 2 regiões"
    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND metric.label.check_id=\"${google_monitoring_uptime_check_config.health[0].uptime_check_id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "600s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.host"]
      }
    }
  }
}

resource "google_monitoring_alert_policy" "server_errors" {
  count = local.monitoring

  display_name          = "LucroMEI teste: respostas 5xx"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.email[0].id]

  conditions {
    display_name = "5 ou mais respostas 5xx em 5 minutos"
    condition_threshold {
      filter          = "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${local.service_name}\" AND metric.label.response_code_class=\"5xx\""
      comparison      = "COMPARISON_GT"
      threshold_value = 4
      duration        = "0s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }
}

resource "google_monitoring_alert_policy" "job_dead" {
  count = local.monitoring

  display_name          = "LucroMEI teste: job desistiu (dead)"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.email[0].id]

  conditions {
    display_name = "Qualquer job dead"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.job_dead[0].name}\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }
}

resource "google_monitoring_alert_policy" "delivery_failed" {
  count = local.monitoring

  display_name          = "LucroMEI teste: falhas de entrega da Meta"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.email[0].id]

  conditions {
    display_name = "3 ou mais recibos failed em 15 minutos"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.delivery_failed[0].name}\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 2
      duration        = "0s"
      aggregations {
        alignment_period     = "900s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }
}

resource "google_monitoring_alert_policy" "queue_lag" {
  count = local.monitoring

  display_name          = "LucroMEI teste: fila atrasada"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.email[0].id]

  conditions {
    display_name = "Job vencido há mais de 5 minutos, por 10 minutos"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.queue_lag[0].name}\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 300
      duration        = "600s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_PERCENTILE_99"
        cross_series_reducer = "REDUCE_MAX"
      }
    }
  }
}
