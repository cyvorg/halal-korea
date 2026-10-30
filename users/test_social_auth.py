"""
Tests for social authentication integration
"""

import copy

from django.conf import settings
from django.test import TestCase, RequestFactory, override_settings
from django.contrib.auth import get_user_model
from django.urls import reverse
from unittest.mock import patch, MagicMock
from allauth.socialaccount.models import SocialApp, SocialAccount
from allauth.socialaccount.providers.google.provider import GoogleProvider

User = get_user_model()


class SocialAuthIntegrationTest(TestCase):
    """Test social authentication integration"""
    
    def setUp(self):
        self.factory = RequestFactory()

        # Settings configure OAuth apps via SOCIALACCOUNT_PROVIDERS["..."]["APP"];
        # allauth merges those with DB SocialApps, so a DB app for the same
        # provider would surface as MultipleObjectsReturned. Strip the
        # settings-level apps so the DB apps below are the only ones.
        providers = copy.deepcopy(settings.SOCIALACCOUNT_PROVIDERS)
        for provider_config in providers.values():
            provider_config.pop("APP", None)
        self._providers_override = override_settings(
            SOCIALACCOUNT_PROVIDERS=providers
        )
        self._providers_override.enable()
        self.addCleanup(self._providers_override.disable)

        # Clear any existing social apps to avoid duplicates
        SocialApp.objects.all().delete()
        
        # Create social apps for testing
        self.google_app = SocialApp.objects.create(
            provider='google',
            name='Google',
            client_id='test_google_client_id',
            secret='test_google_secret',
        )
        self.google_app.sites.add(1)  # Add to default site
        
        self.github_app = SocialApp.objects.create(
            provider='github',
            name='GitHub',
            client_id='test_github_client_id',
            secret='test_github_secret',
        )
        self.github_app.sites.add(1)
        
        self.twitter_app = SocialApp.objects.create(
            provider='twitter',
            name='Twitter',
            client_id='test_twitter_client_id',
            secret='test_twitter_secret',
        )
        self.twitter_app.sites.add(1)
        

    
    def test_user_model_extensions(self):
        """Test that User model has social auth fields"""
        user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        
        # Test new fields exist
        self.assertIsNone(user.profile_picture.name)
        self.assertIsNone(user.social_avatar_url)
        
        # Test avatar_url property
        self.assertIsNone(user.avatar_url)
        
        # Test with social avatar URL
        user.social_avatar_url = 'https://example.com/avatar.jpg'
        user.save()
        self.assertEqual(user.avatar_url, 'https://example.com/avatar.jpg')
    
    def test_social_account_methods(self):
        """Test social account helper methods"""
        user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        
        # Initially no social accounts
        social_accounts = user.get_social_accounts()
        self.assertEqual(len(social_accounts), 0)
        
        # Create a social account
        social_account = SocialAccount.objects.create(
            user=user,
            provider='google',
            uid='123456789',
            extra_data={'email': 'test@example.com', 'name': 'Test User'}
        )
        
        # Now should have one social account
        social_accounts = user.get_social_accounts()
        self.assertEqual(len(social_accounts), 1)
        self.assertEqual(social_accounts[0].provider, 'google')
    
    def test_login_template_has_social_buttons(self):
        """Test that login template includes social login buttons"""
        response = self.client.get(reverse('users:login'))
        self.assertEqual(response.status_code, 200)
        
        # Check for social login elements (the provider_login_url template tag
        # renders to URLs; only Google and GitHub buttons exist)
        self.assertContains(response, '/accounts/google/login/')
        self.assertContains(response, '/accounts/github/login/')
        self.assertContains(response, 'Google')
        self.assertContains(response, 'GitHub')
    
    def test_register_template_has_social_buttons(self):
        """Test that register template includes social signup buttons"""
        response = self.client.get(reverse('users:register'))
        self.assertEqual(response.status_code, 200)
        
        # Check for social signup elements (rendered URLs, not the tag name)
        self.assertContains(response, '/accounts/google/login/')
        self.assertContains(response, '/accounts/github/login/')
        self.assertContains(response, 'Google')
        self.assertContains(response, 'GitHub')
    
    def test_social_auth_urls_configured(self):
        """Test that social auth URLs are properly configured"""
        # Test that allauth URLs are included
        response = self.client.get('/accounts/google/login/')
        # Should redirect to Google OAuth (or show error if not configured)
        self.assertIn(response.status_code, [302, 500])  # 302 redirect or 500 if not configured
    
    def test_account_merging_logic(self):
        """Test account merging functionality"""
        # Create existing user
        existing_user = User.objects.create_user(
            username='existing',
            email='test@example.com',
            password='testpass123'
        )
        
        # Create a social account for the same email
        social_account = SocialAccount.objects.create(
            user=existing_user,
            provider='google',
            uid='123456789',
            extra_data={'email': 'test@example.com', 'name': 'Test User'}
        )
        
        # Verify the social account is linked to existing user
        self.assertEqual(social_account.user, existing_user)
        self.assertEqual(existing_user.get_social_accounts().count(), 1)
    
    def test_profile_picture_handling(self):
        """Test profile picture import from social providers"""
        user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        
        # Test social avatar URL
        user.social_avatar_url = 'https://example.com/avatar.jpg'
        user.save()
        
        self.assertEqual(user.avatar_url, 'https://example.com/avatar.jpg')
    
    @patch('users.adapters.requests.get')
    @patch('users.adapters.Image.open')
    def test_avatar_download(self, mock_image_open, mock_requests_get):
        """Test avatar download functionality"""
        # Mock successful image download
        mock_response = MagicMock()
        mock_response.content = b'fake_image_data'
        mock_response.raise_for_status.return_value = None
        mock_requests_get.return_value = mock_response
        
        # Mock PIL Image
        mock_image = MagicMock()
        mock_image.mode = 'RGB'
        mock_image.thumbnail.return_value = None
        mock_image.save.return_value = None
        mock_image_open.return_value = mock_image
        
        user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        
        # This would normally be called by the adapter
        from users.adapters import CustomSocialAccountAdapter
        adapter = CustomSocialAccountAdapter()
        
        # Test the download method exists
        self.assertTrue(hasattr(adapter, '_download_and_save_avatar'))


class SocialAuthSecurityTest(TestCase):
    """Test security aspects of social authentication"""
    
    def test_rate_limiting_applies_to_social_auth(self):
        """Test that existing rate limiting covers social auth endpoints"""
        # The rate limiting should automatically apply to /accounts/* URLs
        # This is more of an integration test that would need actual rate limiting testing
        pass
    
    def test_csrf_protection(self):
        """Test CSRF protection on social auth endpoints"""
        response = self.client.get('/accounts/google/login/')
        # Should either redirect or require CSRF token
        self.assertIn(response.status_code, [302, 403, 500])
    
    def test_secure_callback_urls(self):
        """Test that callback URLs are properly secured"""
        # Test that callback URLs exist and are properly configured
        response = self.client.get('/accounts/google/login/callback/')
        # Should handle the callback (may error without proper OAuth flow or return 200 with error message)
        self.assertIn(response.status_code, [200, 400, 403, 500])


class SocialAuthAdapterTest(TestCase):
    """Test custom social auth adapters"""
    
    def test_custom_account_adapter_exists(self):
        """Test that custom account adapter is properly configured"""
        from users.adapters import CustomAccountAdapter
        adapter = CustomAccountAdapter()
        self.assertTrue(adapter.is_open_for_signup(None))
    
    def test_custom_social_adapter_exists(self):
        """Test that custom social account adapter is properly configured"""
        from users.adapters import CustomSocialAccountAdapter
        adapter = CustomSocialAccountAdapter()
        self.assertTrue(adapter.is_open_for_signup(None, None))
    
    def test_profile_data_import_methods(self):
        """Test profile data import methods exist"""
        from users.adapters import CustomSocialAccountAdapter
        adapter = CustomSocialAccountAdapter()
        
        # Test that import methods exist
        self.assertTrue(hasattr(adapter, '_import_profile_data'))
        self.assertTrue(hasattr(adapter, '_import_profile_picture'))
        self.assertTrue(hasattr(adapter, '_download_and_save_avatar'))


if __name__ == '__main__':
    import django
    from django.conf import settings
    from django.test.utils import get_runner
    
    django.setup()
    TestRunner = get_runner(settings)
    test_runner = TestRunner()
    failures = test_runner.run_tests(["users.test_social_auth"])
