"""
Tests for monitoring management commands.
"""
from django.test import TestCase, override_settings
from django.core.management import call_command
from django.core.cache import cache
from django.contrib.auth import get_user_model
from django.utils import timezone
from utils.models import RequestLog, SystemMetric, AlertRule
from utils.management.commands.check_monitoring_health import Command as HealthCommand
from io import StringIO
from unittest.mock import patch

User = get_user_model()


class AggregateMetricsCommandTests(TestCase):
    """Test aggregate_metrics management command."""
    
    def setUp(self):
        # Clear RequestLog/SystemMetric rows committed by monitoring
        # middleware threads from earlier tests (outside test transactions).
        RequestLog.objects.all().delete()
        SystemMetric.objects.all().delete()
        self.user = User.objects.create_user(
            username='testuser',
            password='testpass'
        )
    
    def test_aggregate_metrics_command_runs(self):
        """Test that aggregate_metrics command runs successfully."""
        # Create some request logs
        now = timezone.now()
        for i in range(10):
            RequestLog.objects.create(
                path='/test/',
                method='GET',
                status_code=200,
                response_time_ms=100 + i * 10,
                ip_hash='test',
                timestamp=now
            )
        
        out = StringIO()
        call_command('aggregate_metrics', stdout=out)
        
        output = out.getvalue()
        self.assertIn('aggregation', output.lower())
    
    def test_aggregate_metrics_creates_system_metrics(self):
        """Test that aggregation creates system metrics."""
        # Create request logs
        now = timezone.now()
        for i in range(5):
            RequestLog.objects.create(
                path='/test/', method='GET', status_code=200,
                response_time_ms=100, ip_hash='test', timestamp=now
            )
        
        initial_count = SystemMetric.objects.count()
        
        call_command('aggregate_metrics', '--hours', '1', stdout=StringIO())
        
        # Should have created metrics
        self.assertGreater(SystemMetric.objects.count(), initial_count)
    
    def test_aggregate_metrics_with_cleanup(self):
        """Test aggregate_metrics with cleanup option."""
        # Create old request logs
        old_time = timezone.now() - timezone.timedelta(days=60)
        RequestLog.objects.create(
            path='/old/', method='GET', status_code=200,
            response_time_ms=100, ip_hash='test', user_agent_hash='test',
            timestamp=old_time
        )
        
        out = StringIO()
        call_command('aggregate_metrics', '--cleanup', stdout=out)
        
        output = out.getvalue()
        # Should mention cleanup
        self.assertTrue('cleanup' in output.lower() or 'cleaned' in output.lower() or 'old logs' in output.lower())
    
    def test_aggregate_metrics_with_no_logs(self):
        """Test aggregate_metrics with no logs to process."""
        out = StringIO()
        call_command('aggregate_metrics', stdout=out)
        
        output = out.getvalue()
        # Should handle gracefully
        self.assertIn('0', output)

    def test_aggregate_metrics_writes_heartbeat_with_no_logs(self):
        """Idle runs still write an aggregation_heartbeat SystemMetric."""
        self.assertEqual(SystemMetric.objects.count(), 0)

        call_command('aggregate_metrics', stdout=StringIO())

        heartbeat = SystemMetric.objects.filter(
            metric_type='aggregation_heartbeat',
            metric_name='aggregate_metrics',
        )
        self.assertEqual(heartbeat.count(), 1)
        self.assertEqual(heartbeat.first().value, 0.0)

    def test_aggregate_metrics_writes_heartbeat_with_logs(self):
        """Heartbeat is written alongside normal aggregation."""
        now = timezone.now()
        for _ in range(3):
            RequestLog.objects.create(
                path='/test/', method='GET', status_code=200,
                response_time_ms=100, ip_hash='test', timestamp=now
            )

        call_command('aggregate_metrics', stdout=StringIO())

        heartbeat = SystemMetric.objects.get(
            metric_type='aggregation_heartbeat',
            metric_name='aggregate_metrics',
        )
        self.assertEqual(heartbeat.value, 3.0)


class CheckAlertsCommandTests(TestCase):
    """Test check_alerts management command."""
    
    def test_check_alerts_command_runs(self):
        """Test that check_alerts command runs successfully."""
        out = StringIO()
        call_command('check_alerts', stdout=out)
        
        # Should complete without error
        self.assertEqual(out.getvalue(), '')  # Quiet mode by default
    
    def test_check_alerts_verbose_mode(self):
        """Test check_alerts in verbose mode."""
        out = StringIO()
        call_command('check_alerts', '--verbose', stdout=out)
        
        output = out.getvalue()
        self.assertIn('alert', output.lower())
    
    def test_check_alerts_with_triggerable_rule(self):
        """Test check_alerts with a rule that triggers."""
        # Create alert rule
        AlertRule.objects.create(
            name='Test Alert',
            condition='error_rate_above',
            threshold=10.0,
            window_minutes=10,
            alert_channels=['in_app'],
            enabled=True
        )
        
        # Create logs that trigger the alert (50% error rate)
        now = timezone.now()
        for i in range(10):
            RequestLog.objects.create(
                path='/test/', method='GET', status_code=500,
                response_time_ms=100, ip_hash='test', timestamp=now
            )
        for i in range(10):
            RequestLog.objects.create(
                path='/test/', method='GET', status_code=200,
                response_time_ms=100, ip_hash='test', timestamp=now
            )
        
        out = StringIO()
        call_command('check_alerts', '--verbose', stdout=out)
        
        output = out.getvalue()
        # Should mention triggered alert
        self.assertTrue('triggered' in output.lower() or 'alert' in output.lower())


class CheckMonitoringHealthCommandTests(TestCase):
    """Test check_monitoring_health management command."""

    def setUp(self):
        cache.clear()
        # See AggregateMetricsCommandTests: drop rows leaked by monitoring
        # middleware threads from earlier tests.
        RequestLog.objects.all().delete()
        SystemMetric.objects.all().delete()
    
    def test_check_monitoring_health_command_runs(self):
        """Test that check_monitoring_health command runs successfully."""
        out = StringIO()
        call_command('check_monitoring_health', stdout=out)
        
        output = out.getvalue()
        self.assertIn('health', output.lower())
    
    def test_check_monitoring_health_with_recent_metrics(self):
        """Test health check with recent metrics."""
        # Create recent request log
        RequestLog.objects.create(
            path='/test/', method='GET', status_code=200,
            response_time_ms=100, ip_hash='test', user_agent_hash='test',
            timestamp=timezone.now()
        )
        
        out = StringIO()
        call_command('check_monitoring_health', stdout=out)
        
        output = out.getvalue()
        # Should indicate healthy or show some health status
        self.assertTrue('health' in output.lower())

    def test_metrics_freshness_ok_when_idle_no_metrics(self):
        """Idle site (no logs, no metrics) should not warn about freshness."""
        cmd = HealthCommand()
        result = cmd._check_metrics_freshness(hours=1)
        self.assertEqual(result['status'], 'ok')
        self.assertIn('idle', result['message'].lower())

    def test_metrics_freshness_warns_when_logs_exist_but_metrics_stale(self):
        """Warn only when request logs exist but metrics are missing/stale."""
        RequestLog.objects.create(
            path='/test/', method='GET', status_code=200,
            response_time_ms=100, ip_hash='test', user_agent_hash='test',
            timestamp=timezone.now()
        )
        cmd = HealthCommand()
        result = cmd._check_metrics_freshness(hours=1)
        self.assertEqual(result['status'], 'warning')
        self.assertIn('No recent system metrics', result['message'])

    def test_metrics_freshness_ok_with_heartbeat(self):
        """Heartbeat metrics satisfy freshness even with request logs."""
        now = timezone.now()
        RequestLog.objects.create(
            path='/test/', method='GET', status_code=200,
            response_time_ms=100, ip_hash='test', user_agent_hash='test',
            timestamp=now
        )
        SystemMetric.objects.create(
            timestamp=now,
            metric_type='aggregation_heartbeat',
            metric_name='aggregate_metrics',
            value=1.0,
        )
        cmd = HealthCommand()
        result = cmd._check_metrics_freshness(hours=1)
        self.assertEqual(result['status'], 'ok')

    @override_settings(
        ALERT_TELEGRAM_ENABLED=True,
        ALERT_EMAIL_ENABLED=False,
        MONITORING_HEALTH_ALERT_COOLDOWN_WARNING_HOURS=6,
        MONITORING_HEALTH_ALERT_COOLDOWN_CRITICAL_HOURS=1,
    )
    @patch('utils.management.commands.check_monitoring_health.send_telegram_notification')
    def test_health_alert_cooldown_suppresses_duplicate(self, mock_telegram):
        """Same warning issue set is only sent once within cooldown window."""
        cmd = HealthCommand()
        messages = ['No recent system metrics - Run aggregate_metrics command']

        cmd._send_health_alert('warning', messages)
        cmd._send_health_alert('warning', messages)

        self.assertEqual(mock_telegram.call_count, 1)

    @override_settings(
        ALERT_TELEGRAM_ENABLED=True,
        ALERT_EMAIL_ENABLED=False,
        MONITORING_HEALTH_ALERT_COOLDOWN_WARNING_HOURS=6,
    )
    @patch('utils.management.commands.check_monitoring_health.send_telegram_notification')
    def test_health_alert_new_issue_set_bypasses_cooldown(self, mock_telegram):
        """Content-hash means a different warning set still alerts."""
        cmd = HealthCommand()

        cmd._send_health_alert('warning', ['Issue A'])
        cmd._send_health_alert('warning', ['Issue B'])

        self.assertEqual(mock_telegram.call_count, 2)

    @override_settings(
        ALERT_TELEGRAM_ENABLED=True,
        ALERT_EMAIL_ENABLED=False,
        MONITORING_HEALTH_ALERT_COOLDOWN_CRITICAL_HOURS=1,
    )
    @patch('utils.management.commands.check_monitoring_health.send_telegram_notification')
    def test_health_alert_uses_formatter_hashtags(self, mock_telegram):
        """Health alerts use shared formatter with monitoring hashtags."""
        cmd = HealthCommand()
        cmd._send_health_alert('critical', ['No request logs in the last 1 hour(s)'])

        self.assertTrue(mock_telegram.called)
        sent = mock_telegram.call_args[0][0]
        self.assertIn('#halalkorea', sent)
        self.assertIn('#monitoring', sent)
        self.assertIn('#critical', sent)
        self.assertIn('<code>CRITICAL</code>', sent)


class SendDailyDigestCommandTests(TestCase):
    """Test send_daily_digest management command."""
    
    @override_settings(ALERT_EMAIL_ENABLED=False)
    def test_send_daily_digest_disabled(self):
        """Test send_daily_digest when email alerts are disabled."""
        out = StringIO()
        call_command('send_daily_digest', stdout=out)
        
        output = out.getvalue()
        # Should indicate disabled or not sent
        self.assertTrue('disabled' in output.lower() or 'not' in output.lower())
    
    @override_settings(ALERT_EMAIL_ENABLED=True, ALERT_EMAIL_RECIPIENTS=['test@example.com'])
    def test_send_daily_digest_with_recipients(self):
        """Test that command runs with recipients configured."""
        out = StringIO()
        call_command('send_daily_digest', stdout=out)
        
        output = out.getvalue()
        # Should attempt to send or complete
        self.assertTrue(len(output) > 0)


class CommandIntegrationTests(TestCase):
    """Integration tests for commands working together."""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            password='testpass',
            is_staff=True
        )
    
    def test_full_monitoring_workflow(self):
        """Test complete monitoring workflow."""
        # 1. Create request logs
        now = timezone.now()
        for i in range(5):
            RequestLog.objects.create(
                path='/test/', method='GET', status_code=200,
                response_time_ms=100, ip_hash='test', user_agent_hash='test',
                timestamp=now
            )
        
        # 2. Aggregate metrics
        call_command('aggregate_metrics', stdout=StringIO())
        
        # Should have created metrics
        self.assertGreater(SystemMetric.objects.count(), 0)
        
        # 3. Check alerts
        call_command('check_alerts', stdout=StringIO())
        
        # Should complete without error
        self.assertTrue(True)
    
    def test_commands_handle_empty_database(self):
        """Test that commands handle empty database gracefully."""
        # Commands should work with no data
        call_command('aggregate_metrics', stdout=StringIO())
        call_command('check_alerts', stdout=StringIO())
        call_command('check_monitoring_health', stdout=StringIO())
        
        # Should complete without errors
        self.assertTrue(True)
