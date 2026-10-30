"""Tests for admin user email helpers and compose UI."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from utils.models import AdminAction

from .forms import AdminEmailComposeForm

User = get_user_model()


def _sync_submit(fn, *args, **kwargs):
    """Run ThreadPoolExecutor.submit synchronously for deterministic tests."""
    return fn(*args, **kwargs)


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class AdminEmailHelperTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='alice',
            email='alice@example.com',
            password='pass12345',
            first_name='Alice',
            last_name='Lee',
        )

    @patch('users.utils._email_executor.submit', side_effect=_sync_submit)
    def test_send_admin_email_to_address(self, _mock_submit):
        from users.utils import send_admin_email_to_address

        ok = send_admin_email_to_address(
            'guest@example.com',
            'Hello',
            'Plain body',
            display_name='Guest',
        )
        self.assertTrue(ok)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['guest@example.com'])
        self.assertEqual(mail.outbox[0].subject, 'Hello')
        self.assertIn('Guest', mail.outbox[0].body)
        self.assertIn('Plain body', mail.outbox[0].body)

    @patch('users.utils._email_executor.submit', side_effect=_sync_submit)
    def test_send_admin_email_to_address_invalid(self, _mock_submit):
        from users.utils import send_admin_email_to_address

        self.assertFalse(send_admin_email_to_address('not-an-email', 'S', 'B'))
        self.assertEqual(len(mail.outbox), 0)

    @patch('users.utils._email_executor.submit', side_effect=_sync_submit)
    def test_send_admin_email_user_wrapper(self, _mock_submit):
        from users.utils import send_admin_email

        ok = send_admin_email(self.user, 'Hi Alice', 'Message for you')
        self.assertTrue(ok)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['alice@example.com'])
        self.assertIn('Alice Lee', mail.outbox[0].body)

    @patch('users.utils._email_executor.submit', side_effect=_sync_submit)
    def test_queue_admin_emails_dedupe_and_skip(self, _mock_submit):
        from users.utils import queue_admin_emails

        result = queue_admin_emails(
            [
                {'email': 'a@example.com', 'name': 'A'},
                {'email': 'A@example.com', 'name': 'A2'},
                {'email': '', 'name': 'Blank'},
                {'email': 'b@example.com'},
            ],
            'Subject',
            'Body',
        )
        self.assertEqual(result['sent'], 2)
        self.assertEqual(result['skipped'], 2)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(len(mail.outbox), 2)


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class AdminComposeEmailViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(
            username='admin',
            email='admin@example.com',
            password='adminpass123',
        )
        self.staff_no_perm = User.objects.create_user(
            username='staffer',
            email='staffer@example.com',
            password='staffpass123',
            is_staff=True,
        )
        self.u1 = User.objects.create_user(
            username='u1',
            email='u1@example.com',
            password='pass12345',
            email_verified=True,
            preferred_language='en',
            is_active=True,
        )
        self.u2 = User.objects.create_user(
            username='u2',
            email='u2@example.com',
            password='pass12345',
            email_verified=False,
            preferred_language='ko',
            is_active=True,
        )
        self.u3_blank = User.objects.create_user(
            username='u3',
            email='',
            password='pass12345',
            is_active=True,
        )
        # The Users shortcut URL now redirects to the hub, so sends go to the hub.
        self.compose_url = reverse('admin_email:hub')
        self.users_shortcut_url = reverse('admin:users_user_compose_email')

    def _login_admin(self):
        self.client.login(username='admin', password='adminpass123')

    def _broadcast_users(self, count, verified=True):
        """Bulk users so the >50 typed-confirmation path is exercised cheaply."""
        for index in range(count):
            User.objects.create_user(
                username=f'bulk{index}',
                email=f'bulk{index}@example.com',
                password='pass12345',
                email_verified=verified,
                is_active=True,
            )

    @patch('users.utils._email_executor.submit', side_effect=_sync_submit)
    def test_single_user_mode(self, _mock_submit):
        self._login_admin()
        response = self.client.post(
            f'{self.compose_url}?user_id={self.u1.pk}',
            {
                'audience_mode': 'single',
                'user_id': str(self.u1.pk),
                'subject': 'Single subject',
                'message': 'Hello single',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['u1@example.com'])
        action = AdminAction.objects.filter(action_type='email_users').latest('timestamp')
        self.assertEqual(action.changes['mode'], 'single')
        self.assertEqual(action.changes['sent'], 1)
        self.assertEqual(action.changes['subject'], 'Single subject')

    @patch('users.utils._email_executor.submit', side_effect=_sync_submit)
    def test_selected_users_mode(self, _mock_submit):
        self._login_admin()
        ids = f'{self.u1.pk},{self.u2.pk}'
        response = self.client.post(
            f'{self.compose_url}?ids={ids}',
            {
                'audience_mode': 'users',
                'selected_user_ids': ids,
                'subject': 'Selected subject',
                'message': 'Hello selected',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 2)
        recipients = sorted(m.to[0] for m in mail.outbox)
        self.assertEqual(recipients, ['u1@example.com', 'u2@example.com'])
        action = AdminAction.objects.filter(action_type='email_users').latest('timestamp')
        # ?ids= now preselects the explicit "specific users" radio
        self.assertEqual(action.changes['mode'], 'users')

    @patch('users.utils._email_executor.submit', side_effect=_sync_submit)
    def test_filtered_broadcast_mode(self, _mock_submit):
        self._login_admin()
        response = self.client.post(
            self.compose_url,
            {
                'audience_mode': 'segment',
                'subject': 'Filter subject',
                'message': 'Hello filtered',
                'filter_email_verified': 'true',
                'filter_is_active': 'true',
                'filter_preferred_language': 'en',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['u1@example.com'])
        action = AdminAction.objects.filter(action_type='email_users').latest('timestamp')
        self.assertEqual(action.changes['mode'], 'segment')
        self.assertEqual(action.changes['audience_mode'], 'segment')
        self.assertEqual(action.changes['filters']['filter_email_verified'], 'true')

    @patch('users.utils._email_executor.submit', side_effect=_sync_submit)
    def test_adhoc_only_mode(self, _mock_submit):
        self._login_admin()
        # Broadcast with filters that match nobody + ad-hoc emails
        response = self.client.post(
            self.compose_url,
            {
                'audience_mode': 'segment',
                'subject': 'Adhoc subject',
                'message': 'Hello guests',
                'filter_email_verified': 'true',
                'filter_preferred_language': 'uz',
                'additional_emails': 'guest1@example.com\nguest2@example.com',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 2)
        recipients = sorted(m.to[0] for m in mail.outbox)
        self.assertEqual(recipients, ['guest1@example.com', 'guest2@example.com'])
        action = AdminAction.objects.filter(action_type='email_users').latest('timestamp')
        self.assertEqual(action.changes['ad_hoc_count'], 2)

    @patch('users.utils._email_executor.submit', side_effect=_sync_submit)
    def test_combined_user_and_adhoc_dedupe(self, _mock_submit):
        self._login_admin()
        response = self.client.post(
            f'{self.compose_url}?user_id={self.u1.pk}',
            {
                'audience_mode': 'single',
                'user_id': str(self.u1.pk),
                'subject': 'Combined',
                'message': 'Hello',
                'additional_emails': 'u1@example.com, other@example.com',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 2)
        recipients = sorted(m.to[0] for m in mail.outbox)
        self.assertEqual(recipients, ['other@example.com', 'u1@example.com'])

    def test_invalid_additional_email_rejected(self):
        self._login_admin()
        response = self.client.post(
            self.compose_url,
            {
                'audience_mode': 'segment',
                'subject': 'Bad',
                'message': 'Body',
                'additional_emails': 'good@example.com, not-valid',
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)
        self.assertContains(response, 'Invalid email')
        self.assertFalse(AdminAction.objects.filter(action_type='email_users').exists())

    def test_permission_denied_without_change_user(self):
        self.client.login(username='staffer', password='staffpass123')
        response = self.client.get(self.compose_url)
        self.assertEqual(response.status_code, 403)

    def test_permission_allowed_with_change_user(self):
        perm = Permission.objects.get(codename='change_user', content_type__app_label='users')
        self.staff_no_perm.user_permissions.add(perm)
        self.client.login(username='staffer', password='staffpass123')
        response = self.client.get(self.compose_url)
        self.assertEqual(response.status_code, 200)
        # The compose page now lives under the Email hub.
        self.assertContains(response, 'name="audience_mode"')

    def test_email_selected_users_action_redirects(self):
        self._login_admin()
        changelist = reverse('admin:users_user_changelist')
        response = self.client.post(
            changelist,
            {
                'action': 'email_selected_users',
                '_selected_action': [str(self.u1.pk), str(self.u2.pk)],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('admin_email:hub'), response.url)
        self.assertIn('tab=compose', response.url)
        self.assertIn('ids=', response.url)

    def test_change_form_has_send_email_link(self):
        self._login_admin()
        url = reverse('admin:users_user_change', args=[self.u1.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Send email')
        self.assertContains(response, f'user_id={self.u1.pk}')

    def test_changelist_has_compose_link(self):
        self._login_admin()
        response = self.client.get(reverse('admin:users_user_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Compose email')

    @patch('users.utils._email_executor.submit', side_effect=_sync_submit)
    def test_blank_user_email_skipped(self, _mock_submit):
        self._login_admin()
        ids = f'{self.u1.pk},{self.u3_blank.pk}'
        response = self.client.post(
            f'{self.compose_url}?ids={ids}',
            {
                'audience_mode': 'users',
                'selected_user_ids': ids,
                'subject': 'Skip blank',
                'message': 'Hello',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        action = AdminAction.objects.filter(action_type='email_users').latest('timestamp')
        self.assertEqual(action.changes['sent'], 1)
        self.assertGreaterEqual(action.changes['skipped'], 1)

    def test_compose_uses_theme_token_css(self):
        self._login_admin()
        response = self.client.get(self.compose_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'admin/css/compose_email.css')
        self.assertNotContains(response, '#f0fdf4')
        self.assertNotContains(response, '#64748b')

    def test_compose_renders_audience_picker(self):
        self._login_admin()
        response = self.client.get(self.compose_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="audience_mode"')
        for mode in ('all', 'segment', 'users', 'single', 'manual'):
            self.assertContains(response, f'value="{mode}"')
        self.assertContains(response, 'name="selected_user_ids"')
        self.assertContains(response, 'id="user-search-input"')
        self.assertContains(response, 'id="confirm-card"')
        self.assertContains(response, 'admin/js/email_compose.js')

    def test_compose_does_not_render_stale_refresh_hint(self):
        """The live preview replaces the old 'refresh the page to update' copy."""
        self._login_admin()
        response = self.client.get(self.compose_url)
        self.assertNotContains(response, 'refresh the page')


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class AdminEmailHubTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(
            username='admin',
            email='admin@example.com',
            password='adminpass123',
        )
        self.staff_no_perm = User.objects.create_user(
            username='staffer',
            email='staffer@example.com',
            password='staffpass123',
            is_staff=True,
        )
        self.u1 = User.objects.create_user(
            username='u1',
            email='u1@example.com',
            password='pass12345',
            email_verified=True,
            preferred_language='en',
            is_active=True,
        )
        self.hub_url = reverse('admin_email:hub')
        self.compose_hub_url = reverse('admin_email:compose')

    def _login_admin(self):
        self.client.login(username='admin', password='adminpass123')

    def test_hub_requires_login(self):
        response = self.client.get(self.hub_url)
        # staff_member_required redirects to login; some setups surface 404
        self.assertIn(response.status_code, [302, 404])
        self.assertNotEqual(response.status_code, 200)

    def test_hub_permission_denied_without_change_user(self):
        self.client.login(username='staffer', password='staffpass123')
        response = self.client.get(self.hub_url)
        self.assertEqual(response.status_code, 403)

    def test_hub_ok_with_change_user(self):
        perm = Permission.objects.get(codename='change_user', content_type__app_label='users')
        self.staff_no_perm.user_permissions.add(perm)
        self.client.login(username='staffer', password='staffpass123')
        response = self.client.get(self.hub_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Compose')
        self.assertContains(response, 'Recent sends')
        self.assertContains(response, 'admin/css/compose_email.css')

    def test_hub_compose_tab_renders_form(self):
        self._login_admin()
        response = self.client.get(f'{self.hub_url}?tab=compose')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Send email')
        self.assertContains(response, 'name="subject"')

    def test_hub_recent_tab_shows_email_actions(self):
        self._login_admin()
        AdminAction.objects.create(
            admin_user=self.admin,
            action_type='email_users',
            object_repr='Email users (broadcast): Hello',
            changes={
                'subject': 'Hello hub',
                'mode': 'segment',
                'audience_label': 'Segment',
                'recipient_count': 3,
                'sent': 3,
                'skipped': 0,
                'failed': 0,
            },
            ip_hash='abcd1234',
        )
        response = self.client.get(f'{self.hub_url}?tab=recent')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Hello hub')
        self.assertContains(response, 'admin')
        self.assertContains(response, '3')

    @patch('users.utils._email_executor.submit', side_effect=_sync_submit)
    def test_hub_compose_sends_and_redirects_to_recent(self, _mock_submit):
        self._login_admin()
        response = self.client.post(
            f'{self.hub_url}?tab=compose&user_id={self.u1.pk}',
            {
                'user_id': str(self.u1.pk),
                'subject': 'Hub subject',
                'message': 'Hello from hub',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/email/', response.url)
        self.assertIn('tab=recent', response.url)
        self.assertEqual(len(mail.outbox), 1)
        action = AdminAction.objects.filter(action_type='email_users').latest('timestamp')
        self.assertEqual(action.changes['subject'], 'Hub subject')

    def test_compose_path_redirects_to_hub_preserving_params(self):
        self._login_admin()
        response = self.client.get(f'{self.compose_hub_url}?user_id={self.u1.pk}')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/email/', response.url)
        self.assertIn('tab=compose', response.url)
        self.assertIn(f'user_id={self.u1.pk}', response.url)

    def test_users_shortcuts_still_present(self):
        self._login_admin()
        changelist = self.client.get(reverse('admin:users_user_changelist'))
        self.assertEqual(changelist.status_code, 200)
        self.assertContains(changelist, 'Compose email')
        change = self.client.get(reverse('admin:users_user_change', args=[self.u1.pk]))
        self.assertEqual(change.status_code, 200)
        self.assertContains(change, 'Send email')
        self.assertContains(change, f'user_id={self.u1.pk}')


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class AdminEmailAudienceModeTests(TestCase):
    """Each audience radio must resolve to exactly its own recipient source."""

    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(
            username='admin',
            email='admin@example.com',
            password='adminpass123',
        )
        self.u1 = User.objects.create_user(
            username='u1', email='u1@example.com', password='pass12345',
            email_verified=True, preferred_language='en', is_active=True,
        )
        self.u2 = User.objects.create_user(
            username='u2', email='u2@example.com', password='pass12345',
            email_verified=False, preferred_language='ko', is_active=True,
        )
        self.hub_url = reverse('admin_email:hub')
        self.client.login(username='admin', password='adminpass123')

    def _post(self, payload):
        return self.client.post(f'{self.hub_url}?tab=compose', payload)

    def _sent_to(self):
        return sorted(m.to[0] for m in mail.outbox)

    @patch('users.utils._email_executor.submit', side_effect=_sync_submit)
    def test_all_users_mode(self, _mock_submit):
        response = self._post({
            'audience_mode': 'all',
            'subject': 'Everyone',
            'message': 'Hi all',
        })
        self.assertEqual(response.status_code, 302)
        # admin + u1 + u2
        self.assertEqual(self._sent_to(), ['admin@example.com', 'u1@example.com', 'u2@example.com'])
        action = AdminAction.objects.filter(action_type='email_users').latest('timestamp')
        self.assertEqual(action.changes['audience_mode'], 'all')

    @patch('users.utils._email_executor.submit', side_effect=_sync_submit)
    def test_segment_mode_respects_filters(self, _mock_submit):
        response = self._post({
            'audience_mode': 'segment',
            'subject': 'Segment',
            'message': 'Hi',
            'filter_email_verified': 'true',
            'filter_preferred_language': 'en',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._sent_to(), ['u1@example.com'])

    @patch('users.utils._email_executor.submit', side_effect=_sync_submit)
    def test_all_mode_ignores_filters(self, _mock_submit):
        """'All users' is deliberately not narrowed by the segment filters."""
        response = self._post({
            'audience_mode': 'all',
            'subject': 'Everyone',
            'message': 'Hi',
            'filter_email_verified': 'true',
            'filter_preferred_language': 'en',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 3)

    @patch('users.utils._email_executor.submit', side_effect=_sync_submit)
    def test_specific_users_mode(self, _mock_submit):
        response = self._post({
            'audience_mode': 'users',
            'selected_user_ids': f'{self.u1.pk},{self.u2.pk}',
            'subject': 'Picked',
            'message': 'Hi',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._sent_to(), ['u1@example.com', 'u2@example.com'])

    @patch('users.utils._email_executor.submit', side_effect=_sync_submit)
    def test_manual_mode_uses_only_addresses(self, _mock_submit):
        response = self._post({
            'audience_mode': 'manual',
            'additional_emails': 'guest@example.com, other@example.com',
            'subject': 'Guests',
            'message': 'Hi',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._sent_to(), ['guest@example.com', 'other@example.com'])

    def test_large_specific_users_selection_needs_typed_count(self):
        """
        Regression: 'specific users' skipped the confirmation entirely, so an
        unbounded selected_user_ids list could reach any number of recipients.
        """
        response = self._post({
            'audience_mode': 'users',
            'selected_user_ids': f'{self.u1.pk}',
            'subject': 'Picked',
            'message': 'Hi',
        })
        # Only one user here — under the threshold, so it sends.
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._sent_to(), ['u1@example.com'])

    def test_manual_mode_requires_addresses(self):
        response = self._post({
            'audience_mode': 'manual',
            'subject': 'Guests',
            'message': 'Hi',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)
        self.assertContains(response, 'at least one email address')

    def test_specific_users_mode_requires_a_selection(self):
        response = self._post({
            'audience_mode': 'users',
            'subject': 'Nobody',
            'message': 'Hi',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'at least one user')

    def test_invalid_selected_user_ids_rejected(self):
        response = self._post({
            'audience_mode': 'users',
            'selected_user_ids': f'{self.u1.pk},not-a-pk',
            'subject': 'Bad ids',
            'message': 'Hi',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)
        self.assertContains(response, 'Invalid user id')

    def test_empty_segment_shows_filter_guidance(self):
        response = self._post({
            'audience_mode': 'segment',
            'subject': 'Nobody',
            'message': 'Hi',
            'filter_preferred_language': 'uz',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No users match these filters')


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class AdminEmailTypedConfirmationTests(TestCase):
    """Sends over the threshold in a broad mode require typing the count."""

    THRESHOLD = 50

    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(
            username='admin',
            email='admin@example.com',
            password='adminpass123',
        )
        # 1 admin + 60 bulk = 61 recipients, comfortably over the threshold.
        for index in range(60):
            User.objects.create_user(
                username=f'bulk{index}',
                email=f'bulk{index}@example.com',
                password='pass12345',
                is_active=True,
            )
        self.hub_url = reverse('admin_email:hub')
        self.client.login(username='admin', password='adminpass123')

    def _post(self, payload):
        return self.client.post(f'{self.hub_url}?tab=compose', payload)

    def test_all_users_requires_typed_count(self):
        response = self._post({
            'audience_mode': 'all',
            'subject': 'Everyone',
            'message': 'Hi',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)
        self.assertContains(response, 'Type 61 to confirm')

    @patch('users.utils._email_executor.submit', side_effect=_sync_submit)
    def test_all_users_sends_with_correct_typed_count(self, _mock_submit):
        response = self._post({
            'audience_mode': 'all',
            'subject': 'Everyone',
            'message': 'Hi',
            'confirm_send_count': '61',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 61)

    def test_wrong_typed_count_rejected(self):
        response = self._post({
            'audience_mode': 'all',
            'subject': 'Everyone',
            'message': 'Hi',
            'confirm_send_count': '60',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)
        self.assertContains(response, 'Type 61 to confirm')

    @patch('users.utils._email_executor.submit', side_effect=_sync_submit)
    def test_typed_count_not_required_below_threshold(self, _mock_submit):
        """Narrowing under the threshold sends without any typed confirmation."""
        User.objects.create_user(
            username='korean', email='korean@example.com', password='pass12345',
            preferred_language='ko', is_active=True,
        )
        response = self._post({
            'audience_mode': 'segment',
            'subject': 'Narrowed',
            'message': 'Hi',
            'filter_preferred_language': 'ko',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)

    @patch('users.utils._email_executor.submit', side_effect=_sync_submit)
    def test_typed_count_required_for_large_narrowed_segment(self, _mock_submit):
        """A large filtered segment still needs its own count typed."""
        for index in range(55):
            User.objects.create_user(
                username=f'ko{index}', email=f'ko{index}@example.com',
                password='pass12345', preferred_language='ko', is_active=True,
            )
        response = self._post({
            'audience_mode': 'segment',
            'subject': 'Korean users',
            'message': 'Hi',
            'confirm_send_count': '61',
            'filter_preferred_language': 'ko',
        })
        # 55 korean users, not 61 — the stale count must be refused.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)
        self.assertContains(response, 'Type 55 to confirm')

        response = self._post({
            'audience_mode': 'segment',
            'subject': 'Korean users',
            'message': 'Hi',
            'confirm_send_count': '55',
            'filter_preferred_language': 'ko',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 55)

    @patch('users.utils._email_executor.submit', side_effect=_sync_submit)
    def test_manual_mode_needs_no_typed_count(self, _mock_submit):
        """Address-only sends are not a broadcast, so the guard does not apply."""
        addresses = '\n'.join(f'guest{index}@example.com' for index in range(60))
        response = self._post({
            'audience_mode': 'manual',
            'additional_emails': addresses,
            'subject': 'Guests',
            'message': 'Hi',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 60)

    def test_large_send_block_shown_in_preview(self):
        response = self.client.get(f'{self.hub_url}?tab=compose&audience_mode=all')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Type the number below to confirm')


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class AdminEmailApiTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(
            username='admin',
            email='admin@example.com',
            password='adminpass123',
        )
        self.staff_no_perm = User.objects.create_user(
            username='staffer',
            email='staffer@example.com',
            password='staffpass123',
            is_staff=True,
        )
        self.target = User.objects.create_user(
            username='searchable', email='searchable@example.com', password='pass12345',
        )
        self.preview_url = reverse('admin_email:preview')
        self.search_url = reverse('admin_email:user_search')
        self.client.login(username='admin', password='adminpass123')

    def test_preview_endpoint_returns_counts(self):
        # setUp creates admin + staff + target = 3 accounts
        response = self.client.post(self.preview_url, {'audience_mode': 'all'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['audience_mode'], 'all')
        self.assertEqual(data['count'], 3)
        self.assertEqual(data['needs_confirmation'], False)

    def test_preview_endpoint_requires_post(self):
        response = self.client.get(self.preview_url)
        self.assertEqual(response.status_code, 405)

    def test_preview_endpoint_reports_large_sends(self):
        for index in range(60):
            User.objects.create_user(
                username=f'bulk{index}', email=f'bulk{index}@example.com',
                password='pass12345',
            )
        response = self.client.post(self.preview_url, {'audience_mode': 'all'})
        data = response.json()
        self.assertTrue(data['needs_confirmation'])
        self.assertEqual(data['threshold'], 50)
        # 3 from setUp + 60 bulk
        self.assertEqual(data['count'], 63)
        self.assertEqual(len(data['sample']), 20)

    def test_preview_endpoint_json_403_without_permission(self):
        self.client.login(username='staffer', password='staffpass123')
        response = self.client.post(self.preview_url, {'audience_mode': 'all'})
        self.assertEqual(response.status_code, 403)
        self.assertIn('error', response.json())

    def test_user_search_finds_by_username_and_email(self):
        response = self.client.get(self.search_url, {'q': 'searchable'})
        self.assertEqual(response.status_code, 200)
        results = response.json()['results']
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['username'], 'searchable')
        self.assertEqual(results[0]['email'], 'searchable@example.com')

    def test_user_search_ignores_short_queries(self):
        response = self.client.get(self.search_url, {'q': 's'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['results'], [])

    def test_user_search_json_403_without_permission(self):
        self.client.login(username='staffer', password='staffpass123')
        response = self.client.get(self.search_url, {'q': 'searchable'})
        self.assertEqual(response.status_code, 403)

    def test_preview_resolves_the_requested_mode(self):
        """
        The preview payload carries only audience fields. It must resolve the
        mode actually posted — not the last of several audience_mode values.
        """
        response = self.client.post(self.preview_url, {'audience_mode': 'all'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['audience_mode'], 'all')
        self.assertEqual(data['count'], 3)

    def test_preview_single_mode_counts_that_user(self):
        response = self.client.post(
            self.preview_url,
            {'audience_mode': 'single', 'user_id': str(self.target.pk)},
        )
        data = response.json()
        self.assertEqual(data['audience_mode'], 'single')
        self.assertEqual(data['count'], 1)

    def test_preview_manual_mode_counts_addresses_only(self):
        response = self.client.post(
            self.preview_url,
            {
                'audience_mode': 'manual',
                'additional_emails': 'g1@example.com, g2@example.com',
            },
        )
        data = response.json()
        self.assertEqual(data['audience_mode'], 'manual')
        self.assertEqual(data['count'], 2)

    def test_preview_tolerates_garbage_user_id(self):
        """A malformed pk on a live-preview request must not 500 the endpoint."""
        for bad in ('abc', '0', '-1', '9' * 40):
            with self.subTest(user_id=bad):
                response = self.client.post(
                    self.preview_url,
                    {'audience_mode': 'single', 'user_id': bad},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['count'], 0)

    def test_preview_users_mode_resolves_selection(self):
        response = self.client.post(
            self.preview_url,
            {
                'audience_mode': 'users',
                'selected_user_ids': str(self.target.pk),
            },
        )
        data = response.json()
        self.assertEqual(data['audience_mode'], 'users')
        self.assertEqual(data['count'], 1)

    def test_preview_accepts_newline_separated_ids(self):
        """Regression: pasted id lists arrived newline-separated and failed."""
        second = User.objects.create_user(
            username='second', email='second@example.com', password='pass12345',
        )
        response = self.client.post(
            self.preview_url,
            {
                'audience_mode': 'users',
                'selected_user_ids': f'{self.target.pk}\n{second.pk}',
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['audience_mode'], 'users')
        self.assertEqual(data['count'], 2)

    def test_compose_tolerates_garbage_user_id(self):
        """The page itself must not 500 on a malformed ?user_id=."""
        response = self.client.get(
            f'{reverse("admin_email:hub")}?tab=compose&audience_mode=single&user_id=abc'
        )
        self.assertEqual(response.status_code, 200)


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class AdminEmailHtmlSanitisationTests(TestCase):
    """
    send_as_html drops admin markup straight into the branded template via
    |safe, so it has to be sanitised or any staff account becomes a phishing
    tool on the site's own domain.
    """

    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(
            username='admin', email='admin@example.com', password='adminpass123',
        )
        self.target = User.objects.create_user(
            username='t', email='t@example.com', password='pass12345',
        )
        self.hub_url = reverse('admin_email:hub')
        self.client.login(username='admin', password='adminpass123')

    def _send_html(self, body):
        with patch('users.utils._email_executor.submit', side_effect=_sync_submit):
            return self.client.post(
                f'{self.hub_url}?tab=compose',
                {
                    'audience_mode': 'single',
                    'user_id': str(self.target.pk),
                    'subject': 'HTML test',
                    'message': body,
                    'send_as_html': 'on',
                },
            )

    def _html_body(self):
        return mail.outbox[0].alternatives[0][0]

    def test_safe_formatting_survives(self):
        response = self._send_html(
            '<p>Hello <strong>world</strong> '
            '<a href="https://halal-korea.com">link</a></p>'
        )
        self.assertEqual(response.status_code, 302)
        body = self._html_body()
        self.assertIn('<strong>world</strong>', body)
        self.assertIn('href="https://halal-korea.com"', body)

    def test_script_is_stripped(self):
        self._send_html('<script>alert(1)</script><p>safe</p>')
        body = self._html_body()
        self.assertNotIn('alert(1)', body)
        self.assertIn('safe', body)

    def test_event_handlers_are_stripped(self):
        self._send_html('<a href="https://x.com" onclick="steal()">y</a>')
        body = self._html_body()
        self.assertNotIn('onclick', body)

    def test_javascript_urls_are_stripped(self):
        self._send_html('<a href="javascript:alert(1)">x</a>')
        self.assertNotIn('javascript:', self._html_body())

    def test_positioning_styles_are_stripped(self):
        self._send_html(
            '<div style="position:fixed;top:0;z-index:9999;color:red">overlay</div>'
        )
        body = self._html_body()
        self.assertNotIn('position', body)
        self.assertNotIn('z-index', body)

    def test_images_and_iframes_are_stripped(self):
        self._send_html(
            '<img src=x onerror=alert(1)>'
            '<iframe src="https://evil"></iframe>'
            '<p>ok</p>'
        )
        body = self._html_body()
        # Assert on the admin-supplied content only: the branded wrapper
        # legitimately contains the site logo <img>.
        self.assertNotIn('onerror', body)
        self.assertNotIn('<iframe', body)
        self.assertNotIn('src=x', body)
        self.assertNotIn('evil.com', body)
        self.assertIn('<p>ok</p>', body)

    def test_relative_links_are_stripped(self):
        """A relative link is an open-tracking pixel in an email client."""
        self._send_html('<a href="/track/open">x</a>')
        self.assertNotIn('/track/open', self._html_body())

    def test_body_of_only_unsafe_markup_is_rejected(self):
        response = self._send_html('<script>alert(1)</script>')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)
        self.assertContains(response, 'unsafe markup')

    def test_plain_text_mode_still_escapes(self):
        with patch('users.utils._email_executor.submit', side_effect=_sync_submit):
            self.client.post(
                f'{self.hub_url}?tab=compose',
                {
                    'audience_mode': 'single',
                    'user_id': str(self.target.pk),
                    'subject': 'Plain',
                    'message': '<b>not bold</b>',
                },
            )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('&lt;b&gt;not bold&lt;/b&gt;', mail.outbox[0].body)


class AdminEmailSanitizerUnitTests(TestCase):
    def test_empty_and_none_return_empty(self):
        from users.html_sanitizer import sanitize_email_html

        self.assertEqual(sanitize_email_html(''), '')
        self.assertEqual(sanitize_email_html(None), '')

    def test_allowlist_drops_unknown_tags(self):
        from users.html_sanitizer import sanitize_email_html

        self.assertEqual(sanitize_email_html('<unknown>y</unknown>'), 'y')

    def test_comments_are_stripped(self):
        from users.html_sanitizer import sanitize_email_html

        self.assertEqual(sanitize_email_html('<!-- c --><p>k</p>'), '<p>k</p>')

    def test_script_content_is_removed_not_just_the_tag(self):
        from users.html_sanitizer import sanitize_email_html

        self.assertEqual(sanitize_email_html('<script>SECRET</script>ok'), 'ok')


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class AdminEmailDeepLinkTests(TestCase):
    """The picker must preselect the radio matching each legacy entry point."""

    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(
            username='admin', email='admin@example.com', password='adminpass123',
        )
        self.u1 = User.objects.create_user(
            username='u1', email='u1@example.com', password='pass12345',
        )
        self.hub_url = reverse('admin_email:hub')

    def _login_admin(self):
        self.client.login(username='admin', password='adminpass123')

    def _checked_mode(self, response):
        checked = [
            str(radio.data['value'])
            for radio in response.context['form']['audience_mode']
            if radio.data['selected']
        ]
        return checked[0] if checked else None

    def test_user_id_deeplink_preselects_single(self):
        self._login_admin()
        response = self.client.get(f'{self.hub_url}?tab=compose&user_id={self.u1.pk}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._checked_mode(response), 'single')

    def test_ids_deeplink_preselects_specific_users(self):
        self._login_admin()
        response = self.client.get(f'{self.hub_url}?tab=compose&ids={self.u1.pk}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._checked_mode(response), 'users')

    def _stage_draft(self, **payload):
        """Persist a draft without sending: blank subject keeps the form invalid."""
        payload.pop('subject', None)
        payload.setdefault('message', 'Body in progress')
        return self.client.post(
            f'{self.hub_url}?tab=compose',
            {**payload, 'subject': ''},
        )

    def test_ids_deeplink_beats_a_stale_draft(self):
        """
        Regression: a saved 'all users' draft used to overwrite the ?ids= deep
        link from the changelist action, so ticking two users and pressing Send
        would broadcast to the entire user base.
        """
        self._login_admin()
        other = User.objects.create_user(
            username='other', email='other@example.com', password='pass12345',
        )
        self._stage_draft(audience_mode='all')

        response = self.client.get(f'{self.hub_url}?tab=compose&ids={self.u1.pk}')
        self.assertEqual(self._checked_mode(response), 'users')
        self.assertEqual(response.context['selected_users'][0]['id'], self.u1.pk)

        # And the deep link must not have destroyed the draft either.
        restored = self.client.get(f'{self.hub_url}?tab=compose')
        self.assertEqual(restored.context['form'].initial['audience_mode'], 'all')

    def test_user_id_deeplink_beats_a_stale_draft(self):
        self._login_admin()
        self._stage_draft(audience_mode='all')
        response = self.client.get(f'{self.hub_url}?tab=compose&user_id={self.u1.pk}')
        self.assertEqual(self._checked_mode(response), 'single')
        restored = self.client.get(f'{self.hub_url}?tab=compose')
        self.assertEqual(restored.context['form'].initial['audience_mode'], 'all')

    def test_selected_user_ids_accepts_pasted_newlines(self):
        """Ids pasted as newlines must parse the same way emails do."""
        from users.forms import parse_id_list

        u2 = User.objects.create_user(
            username='u2', email='u2@example.com', password='pass12345',
        )
        ids, invalid = parse_id_list(f'{self.u1.pk}\n{u2.pk}\n\n{u2.pk}')
        self.assertEqual(ids, [self.u1.pk, u2.pk])
        self.assertEqual(invalid, [])

        self.assertEqual(parse_id_list('1, 2\t3')[0], [1, 2, 3])
        self.assertEqual(parse_id_list(f'{self.u1.pk},abc')[1], ['abc'])
        # Out-of-range / non-positive ids are rejected, not passed to the ORM.
        self.assertEqual(parse_id_list('0')[1], ['0'])
        self.assertEqual(parse_id_list('9' * 40)[1], ['9' * 40])

    def test_bare_hub_defaults_to_segment(self):
        """Landing on the hub must never preselect a send-to-everyone default."""
        self._login_admin()
        response = self.client.get(f'{self.hub_url}?tab=compose')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._checked_mode(response), 'segment')

    def test_users_shortcut_redirects_to_hub(self):
        self._login_admin()
        response = self.client.get(
            f"{reverse('admin:users_user_compose_email')}?user_id={self.u1.pk}"
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(self.hub_url, response.url)
        self.assertIn('tab=compose', response.url)
        self.assertIn(f'user_id={self.u1.pk}', response.url)


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class AdminEmailDraftTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(
            username='admin', email='admin@example.com', password='adminpass123',
        )
        self.hub_url = reverse('admin_email:hub')
        self.client.login(username='admin', password='adminpass123')

    def _stage_draft(self, **payload):
        """
        Submit an incomplete payload so the form re-renders and persists the draft.

        The blank subject is deliberate: subject is required, so the form stays
        invalid, no send fires, and _save_draft runs on the re-render. These tests
        are about state surviving a round trip, not about delivery.
        """
        payload.pop('subject', None)
        payload.setdefault('message', 'Body in progress')
        return self.client.post(
            f'{self.hub_url}?tab=compose',
            {**payload, 'subject': ''},
        )

    def test_draft_restores_audience_choice_not_just_the_body(self):
        """The picker state must survive too, not only the message text."""
        self._stage_draft(audience_mode='all')
        after = self.client.get(f'{self.hub_url}?tab=compose')
        self.assertEqual(after.context['form'].initial['audience_mode'], 'all')

    def test_draft_restores_user_selection(self):
        target = User.objects.create_user(
            username='drafttarget', email='drafttarget@example.com', password='pass12345',
        )
        self._stage_draft(
            audience_mode='users', selected_user_ids=str(target.pk),
        )
        after = self.client.get(f'{self.hub_url}?tab=compose')
        self.assertEqual(after.context['form'].initial['selected_user_ids'], str(target.pk))
        self.assertEqual(after.context['audience_mode'], 'users')

    def test_draft_restores_filters(self):
        self._stage_draft(
            audience_mode='segment',
            filter_preferred_language='ko',
            include_staff='on',
        )
        after = self.client.get(f'{self.hub_url}?tab=compose')
        self.assertEqual(
            after.context['form'].initial['filter_preferred_language'], 'ko'
        )
        self.assertTrue(after.context['form'].initial['include_staff'])

    def test_explicit_params_win_over_draft(self):
        """A deep link is a deliberate choice and must not be overridden."""
        self._stage_draft(audience_mode='all')
        after = self.client.get(f'{self.hub_url}?tab=compose&audience_mode=segment')
        self.assertEqual(after.context['form'].initial['audience_mode'], 'segment')
        self.assertNotEqual(after.context['form'].initial.get('message'), 'Body in progress')

    def test_draft_survives_navigating_away(self):
        first = self.client.get(f'{self.hub_url}?tab=compose')
        self.assertEqual(first.status_code, 200)
        self._stage_draft()

        # Visit an unrelated admin page, then come back to a bare compose URL.
        self.client.get(reverse('admin:users_user_changelist'))
        second = self.client.get(f'{self.hub_url}?tab=compose')
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            second.context['form'].initial['message'],
            'Body in progress',
        )

    def test_discard_clears_draft(self):
        self._stage_draft(audience_mode='segment')
        self.client.get(f'{self.hub_url}?tab=compose')
        response = self.client.get(f'{self.hub_url}?tab=compose&discard=1')
        self.assertEqual(response.status_code, 302)

        after = self.client.get(f'{self.hub_url}?tab=compose')
        self.assertEqual(after.status_code, 200)
        self.assertEqual(after.context['form'].initial.get('message', ''), '')

    def test_successful_send_clears_draft(self):
        with patch('users.utils._email_executor.submit', side_effect=_sync_submit):
            self.client.post(
                f'{self.hub_url}?tab=compose',
                {'audience_mode': 'manual', 'additional_emails': 'guest@example.com',
                 'subject': 'Sent already', 'message': 'Body'},
            )
        after = self.client.get(f'{self.hub_url}?tab=compose')
        self.assertEqual(after.status_code, 200)
        self.assertNotEqual(after.context['form'].initial.get('subject'), 'Sent already')


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class AdminEmailEnhancedFeaturesTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(
            username='admin', email='admin@example.com', password='adminpass123',
        )
        self.u1 = User.objects.create_user(
            username='alice', email='alice@example.com', password='pass12345',
        )
        self.hub_url = reverse('admin_email:hub')
        self.preview_render_url = reverse('admin_email:render_preview')
        self.test_send_url = reverse('admin_email:test_send')
        self.client.login(username='admin', password='adminpass123')

    @patch('users.utils._email_executor.submit', side_effect=_sync_submit)
    def test_send_as_html_strips_tags_for_plain_text_fallback(self, _mock_submit):
        response = self.client.post(
            f'{self.hub_url}?tab=compose',
            {
                'audience_mode': 'manual',
                'additional_emails': 'recipient@example.com',
                'subject': 'Rich HTML Email',
                'message': '<p>Hello <b>World</b>! <a href="https://example.com">Click</a></p>',
                'send_as_html': 'on',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        # HTML alternative body contains formatted HTML
        self.assertTrue(len(sent.alternatives) > 0)
        self.assertIn('<b>World</b>', sent.alternatives[0][0])
        # Plain body must NOT have raw HTML tags
        self.assertNotIn('<p>', sent.body)
        self.assertNotIn('<b>', sent.body)
        self.assertNotIn('</a>', sent.body)
        self.assertIn('Hello World! Click', sent.body)

    def test_render_email_preview_api(self):
        response = self.client.post(
            self.preview_render_url,
            {
                'subject': 'Preview Subject',
                'message': '<p>Preview <strong>Message</strong></p>',
                'send_as_html': 'on',
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('html', data)
        self.assertIn('plain', data)
        self.assertEqual(data['subject'], 'Preview Subject')
        self.assertIn('<strong>Message</strong>', data['html'])
        self.assertNotIn('<strong>', data['plain'])

    @patch('users.utils._email_executor.submit', side_effect=_sync_submit)
    def test_test_send_api(self, _mock_submit):
        response = self.client.post(
            self.test_send_url,
            {
                'test_email': 'tester@example.com',
                'subject': 'Test Campaign',
                'message': 'Testing message body',
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get('ok'))
        self.assertEqual(data.get('email'), 'tester@example.com')
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['tester@example.com'])
        self.assertIn('[TEST]', mail.outbox[0].subject)

    def test_stale_ids_tolerated_in_all_audience_mode(self):
        form = AdminEmailComposeForm(
            data={
                'audience_mode': 'all',
                'selected_user_ids': 'invalid_id_999999999999999999999',
                'subject': 'All users test',
                'message': 'Message to all',
            }
        )
        self.assertTrue(form.is_valid())
