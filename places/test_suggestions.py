from django.test import TestCase, Client, RequestFactory
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.gis.geos import Point
from io import BytesIO
from PIL import Image
import json
from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.messages.storage.fallback import FallbackStorage

from .models import HalalPlace, PlaceEditSuggestion, PlaceImageSuggestion, BusinessHoursSuggestion
from .forms import PlaceSuggestionForm

User = get_user_model()


class PlaceEditSuggestionModelTest(TestCase):
    """Test the PlaceEditSuggestion model"""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.user.email_verified = True
        self.user.save(update_fields=['email_verified'])
        self.place = HalalPlace.objects.create(
            name='Test Restaurant',
            description='A test restaurant',
            category='restaurant',
            location=Point(127.0, 37.5),
            address='Test Address',
            status='approved',
            submitted_by=self.user
        )
    
    def test_create_edit_suggestion(self):
        """Test creating an edit suggestion"""
        suggestion = PlaceEditSuggestion.objects.create(
            place=self.place,
            suggested_by=self.user,
            field_name='name',
            current_value='Test Restaurant',
            suggested_value='Updated Restaurant',
            reason='The name has changed'
        )
        
        self.assertEqual(suggestion.status, 'pending')
        self.assertEqual(str(suggestion), 'Test Restaurant - Name edit by testuser')
        self.assertEqual(suggestion.place, self.place)
        self.assertEqual(suggestion.suggested_by, self.user)
    
    def test_suggestion_choices(self):
        """Test that field choices are valid"""
        valid_fields = [choice[0] for choice in PlaceEditSuggestion.EDITABLE_FIELDS]
        self.assertIn('name', valid_fields)
        self.assertIn('description', valid_fields)
        self.assertIn('category', valid_fields)
        self.assertIn('address', valid_fields)


class PlaceImageSuggestionModelTest(TestCase):
    """Test the PlaceImageSuggestion model"""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.user.email_verified = True
        self.user.save(update_fields=['email_verified'])
        self.place = HalalPlace.objects.create(
            name='Test Restaurant',
            description='A test restaurant',
            category='restaurant',
            location=Point(127.0, 37.5),
            address='Test Address',
            status='approved',
            submitted_by=self.user
        )
    
    def test_create_image_suggestion(self):
        """Test creating an image suggestion"""
        # Create a test image
        image = Image.new('RGB', (100, 100), color='red')
        img_io = BytesIO()
        image.save(img_io, format='JPEG')
        img_io.seek(0)
        
        uploaded_file = SimpleUploadedFile(
            "test_image.jpg",
            img_io.getvalue(),
            content_type="image/jpeg"
        )
        
        suggestion = PlaceImageSuggestion.objects.create(
            place=self.place,
            suggested_by=self.user,
            image=uploaded_file,
            caption='Test image'
        )
        
        self.assertEqual(suggestion.status, 'pending')
        self.assertEqual(str(suggestion), 'Image for Test Restaurant by testuser')
        self.assertEqual(suggestion.place, self.place)
        self.assertEqual(suggestion.suggested_by, self.user)


class PlaceSuggestionFormTest(TestCase):
    """Test the PlaceSuggestionForm"""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.user.email_verified = True
        self.user.save(update_fields=['email_verified'])
        self.place = HalalPlace.objects.create(
            name='Test Restaurant',
            description='A test restaurant',
            category='restaurant',
            location=Point(127.0, 37.5),
            address='Test Address',
            status='approved',
            submitted_by=self.user
        )
    
    def test_valid_form_with_field_suggestions(self):
        """Test form with valid field suggestions"""
        form_data = {
            'suggest_name': True,
            'name': 'Updated Restaurant Name',
            'suggest_description': True,
            'description': 'Updated description',
            'reason': 'The information has changed'
        }
        
        form = PlaceSuggestionForm(place=self.place, data=form_data)
        self.assertTrue(form.is_valid())
        
        suggestions = form.get_field_suggestions()
        self.assertEqual(len(suggestions), 2)
        self.assertEqual(suggestions[0]['field_name'], 'name')
        self.assertEqual(suggestions[1]['field_name'], 'description')
    
    def test_form_allows_reason_only_submission(self):
        """Form stays valid with only a reason: business-hours suggestions
        arrive as hidden JSON the form cannot see, so the at-least-one-change
        rule is enforced by the view (see SuggestionViewsTest)."""
        form_data = {
            'reason': 'Some reason'
        }

        form = PlaceSuggestionForm(place=self.place, data=form_data)
        self.assertTrue(form.is_valid())
    
    def test_form_requires_value_when_field_suggested(self):
        """Test that form requires value when field is suggested"""
        form_data = {
            'suggest_name': True,
            # Missing 'name' value
            'reason': 'Some reason'
        }
        
        form = PlaceSuggestionForm(place=self.place, data=form_data)
        self.assertFalse(form.is_valid())


class SuggestionViewsTest(TestCase):
    """Test the suggestion views"""
    
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.user.email_verified = True
        self.user.save(update_fields=['email_verified'])
        self.place = HalalPlace.objects.create(
            name='Test Restaurant',
            description='A test restaurant',
            category='restaurant',
            location=Point(127.0, 37.5),
            address='Test Address',
            status='approved',
            submitted_by=self.user
        )
    
    def test_suggest_edit_view_requires_login(self):
        """Test that suggest edit view requires login"""
        url = reverse('places:suggest_place_edit', args=[self.place.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)  # Redirect to login
    
    def test_suggest_edit_view_authenticated(self):
        """Test suggest edit view with authenticated user"""
        self.client.login(username='testuser', password='testpass123')
        url = reverse('places:suggest_place_edit', args=[self.place.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Suggest Edit')
        self.assertContains(response, self.place.name)
    
    def test_suggest_edit_post_valid_data(self):
        """Test posting valid suggestion data"""
        self.client.login(username='testuser', password='testpass123')
        url = reverse('places:suggest_place_edit', args=[self.place.pk])
        
        data = {
            'suggest_name': True,
            'name': 'Updated Restaurant Name',
            'reason': 'The name has changed'
        }
        
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)  # Redirect after success
        
        # Check that suggestion was created
        self.assertEqual(PlaceEditSuggestion.objects.count(), 1)
        suggestion = PlaceEditSuggestion.objects.first()
        self.assertEqual(suggestion.field_name, 'name')
        self.assertEqual(suggestion.suggested_value, 'Updated Restaurant Name')

    def test_suggest_edit_post_without_any_suggestion_rejected(self):
        """View enforces at least one change when nothing is suggested"""
        self.client.login(username='testuser', password='testpass123')
        url = reverse('places:suggest_place_edit', args=[self.place.pk])

        response = self.client.post(url, {'reason': 'Just a reason'}, follow=True)

        self.assertEqual(PlaceEditSuggestion.objects.count(), 0)
        msgs = [str(m) for m in response.context['messages']]
        self.assertTrue(
            any('Please suggest at least one change' in m for m in msgs),
            f"expected an at-least-one-change warning, got: {msgs}",
        )

    def test_suggest_edit_post_business_hours_only_accepted(self):
        """A reason-only form submission carrying a genuine business-hours
        payload is valid and creates a BusinessHoursSuggestion"""
        self.client.login(username='testuser', password='testpass123')
        url = reverse('places:suggest_place_edit', args=[self.place.pk])

        hours_payload = json.dumps({
            'is_24_hours': False,
            'notes': 'Closed for lunch',
            'hours': {'1': [{'open': '09:00', 'close': '18:00'}]},
        })

        response = self.client.post(url, {
            'reason': 'Hours have changed',
            'suggest_business_hours': 'true',
            'business_hours_json': hours_payload,
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(BusinessHoursSuggestion.objects.count(), 1)
        suggestion = BusinessHoursSuggestion.objects.first()
        self.assertEqual(suggestion.place, self.place)
        self.assertEqual(
            suggestion.suggested_hours, {'1': [{'open': '09:00', 'close': '18:00'}]}
        )
        self.assertEqual(PlaceEditSuggestion.objects.count(), 0)
    
    def test_suggest_edit_with_image_upload(self):
        """Test posting suggestion with image upload"""
        self.client.login(username='testuser', password='testpass123')
        url = reverse('places:suggest_place_edit', args=[self.place.pk])
        
        # Create a test image
        image = Image.new('RGB', (100, 100), color='red')
        img_io = BytesIO()
        image.save(img_io, format='JPEG')
        img_io.seek(0)
        
        uploaded_file = SimpleUploadedFile(
            "test_image.jpg",
            img_io.getvalue(),
            content_type="image/jpeg"
        )
        
        data = {
            'reason': 'Adding a photo of the restaurant',
            'images': uploaded_file
        }
        
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)  # Redirect after success
        
        # Check that image suggestion was created
        self.assertEqual(PlaceImageSuggestion.objects.count(), 1)
        suggestion = PlaceImageSuggestion.objects.first()
        self.assertEqual(suggestion.place, self.place)
    
    def test_my_contributions_view_requires_login(self):
        """Test that my contributions view requires login"""
        url = reverse('places:my_contributions')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)  # Redirect to login
    
    def test_my_contributions_view_authenticated(self):
        """Test my contributions view with authenticated user"""
        self.client.login(username='testuser', password='testpass123')
        
        # Create some suggestions
        PlaceEditSuggestion.objects.create(
            place=self.place,
            suggested_by=self.user,
            field_name='name',
            current_value='Test Restaurant',
            suggested_value='Updated Restaurant',
            reason='Name changed'
        )
        
        url = reverse('places:my_contributions')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'My Contributions')
        self.assertContains(response, 'Updated Restaurant')


class AdminSuggestionTest(TestCase):
    """Test admin functionality for suggestions"""
    
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            username='admin',
            email='admin@example.com',
            password='adminpass123'
        )
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.place = HalalPlace.objects.create(
            name='Test Restaurant',
            description='A test restaurant',
            category='restaurant',
            location=Point(127.0, 37.5),
            address='Test Address',
            status='approved',
            submitted_by=self.user
        )
    
    def test_admin_can_approve_field_suggestion(self):
        """Test that admin can approve field suggestions"""
        suggestion = PlaceEditSuggestion.objects.create(
            place=self.place,
            suggested_by=self.user,
            field_name='name',
            current_value='Test Restaurant',
            suggested_value='Updated Restaurant',
            reason='Name changed'
        )
        
        from places.admin import PlaceEditSuggestionAdmin
        admin = PlaceEditSuggestionAdmin(PlaceEditSuggestion, None)
        
        # Test the apply suggestion method
        result = admin._apply_suggestion(suggestion, self.admin_user)
        self.assertTrue(result)
        
        # Check that place was updated
        self.place.refresh_from_db()
        self.assertEqual(self.place.name, 'Updated Restaurant')
    
    def test_admin_can_approve_image_suggestion(self):
        """Test that admin can approve image suggestions"""
        # Create a test image
        image = Image.new('RGB', (100, 100), color='red')
        img_io = BytesIO()
        image.save(img_io, format='JPEG')
        img_io.seek(0)
        
        uploaded_file = SimpleUploadedFile(
            "test_image.jpg",
            img_io.getvalue(),
            content_type="image/jpeg"
        )
        
        suggestion = PlaceImageSuggestion.objects.create(
            place=self.place,
            suggested_by=self.user,
            image=uploaded_file,
            caption='Test image'
        )
        
        from places.admin import PlaceImageSuggestionAdmin
        admin = PlaceImageSuggestionAdmin(PlaceImageSuggestion, None)
        
        # Test the apply image suggestion method
        result = admin._apply_image_suggestion(suggestion, self.admin_user)
        self.assertTrue(result)
        
        # Check that place was updated with image
        self.place.refresh_from_db()
        self.assertIsNotNone(self.place.photo_urls)
        self.assertTrue(len(self.place.photo_urls) > 0)


class SuggestionIntegrationTest(TestCase):
    """Integration tests for the complete suggestion workflow"""
    
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.user.email_verified = True
        self.user.save(update_fields=['email_verified'])
        self.admin_user = User.objects.create_superuser(
            username='admin',
            email='admin@example.com',
            password='adminpass123'
        )
        self.admin_user.email_verified = True
        self.admin_user.save(update_fields=['email_verified'])
        self.place = HalalPlace.objects.create(
            name='Test Restaurant',
            description='A test restaurant',
            category='restaurant',
            location=Point(127.0, 37.5),
            address='Test Address',
            status='approved',
            submitted_by=self.user
        )
    
    def test_complete_suggestion_workflow(self):
        """Test the complete workflow from suggestion to approval"""
        # Step 1: User creates a suggestion
        self.client.login(username='testuser', password='testpass123')
        url = reverse('places:suggest_place_edit', args=[self.place.pk])
        
        data = {
            'suggest_name': True,
            'name': 'Updated Restaurant Name',
            'suggest_description': True,
            'description': 'Updated description text',
            'reason': 'Information has changed'
        }
        
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        
        # Check suggestions were created
        self.assertEqual(PlaceEditSuggestion.objects.count(), 2)
        
        # Step 2: Admin approves suggestions
        suggestions = PlaceEditSuggestion.objects.all()
        
        from places.admin import PlaceEditSuggestionAdmin
        admin = PlaceEditSuggestionAdmin(PlaceEditSuggestion, None)
        
        for suggestion in suggestions:
            admin._apply_suggestion(suggestion, self.admin_user)
            suggestion.status = 'approved'
            suggestion.reviewed_by = self.admin_user
            suggestion.save()
        
        # Step 3: Check that place was updated
        self.place.refresh_from_db()
        self.assertEqual(self.place.name, 'Updated Restaurant Name')
        self.assertEqual(self.place.description, 'Updated description text')
        
        # Step 4: Check that suggestions are marked as approved
        for suggestion in suggestions:
            suggestion.refresh_from_db()
            self.assertEqual(suggestion.status, 'approved')
            self.assertEqual(suggestion.reviewed_by, self.admin_user)
    
    def test_place_detail_shows_suggest_edit_button(self):
        """Test that place detail page shows suggest edit button for authenticated users"""
        # Test without login
        url = reverse('places:place_detail', args=[self.place.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Suggest Edit')
        
        # Test with login
        self.client.login(username='testuser', password='testpass123')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Suggest Edit')


class PlaceImageSuggestionAdminSaveModelTest(TestCase):
    """Test admin save_model workflow for image suggestions"""

    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.user.email_verified = True
        self.user.save(update_fields=['email_verified'])
        self.admin_user = User.objects.create_superuser(
            username='admin',
            email='admin@example.com',
            password='adminpass123'
        )
        self.admin_user.email_verified = True
        self.admin_user.save(update_fields=['email_verified'])
        self.place = HalalPlace.objects.create(
            name='Test Restaurant',
            description='A test restaurant',
            category='restaurant',
            location=Point(127.0, 37.5),
            address='Test Address',
            status='approved',
            submitted_by=self.user
        )
        # Create image suggestion with valid image
        image = Image.new('RGB', (100, 100), color='blue')
        img_io = BytesIO()
        image.save(img_io, format='JPEG')
        img_io.seek(0)
        uploaded_file = SimpleUploadedFile(
            "admin_save_image.jpg",
            img_io.getvalue(),
            content_type="image/jpeg"
        )
        self.suggestion = PlaceImageSuggestion.objects.create(
            place=self.place,
            suggested_by=self.user,
            image=uploaded_file,
            caption='Admin save image'
        )
        from places.admin import PlaceImageSuggestionAdmin
        self.admin = PlaceImageSuggestionAdmin(PlaceImageSuggestion, None)

    def _build_request(self):
        request = self.factory.post('/admin/places/placeimagesuggestion/')
        request.user = self.admin_user
        # Attach session and messages to support Django admin messaging
        session_middleware = SessionMiddleware(lambda req: None)
        session_middleware.process_request(request)
        request.session.save()
        setattr(request, '_messages', FallbackStorage(request))
        return request

    def test_save_model_adds_photo_when_status_approved(self):
        """Approving via save_model adds image to place photos and sets reviewer metadata"""
        request = self._build_request()
        self.suggestion.status = 'approved'

        self.admin.save_model(request, self.suggestion, form=None, change=False)

        self.place.refresh_from_db()
        self.suggestion.refresh_from_db()
        self.assertIsNotNone(self.place.photo_urls)
        self.assertEqual(len(self.place.photo_urls), 1)
        self.assertIn('admin_save_image', self.place.photo_urls[0])
        self.assertEqual(self.suggestion.reviewed_by, self.admin_user)
        self.assertIsNotNone(self.suggestion.reviewed_at)

    def test_save_model_no_duplicate_on_reapproval(self):
        """Editing an already-approved suggestion keeps photo list stable"""
        request = self._build_request()
        self.suggestion.status = 'approved'
        self.admin.save_model(request, self.suggestion, form=None, change=False)

        initial_photo_urls = list(self.place.photo_urls)

        second_request = self._build_request()
        self.admin.save_model(second_request, self.suggestion, form=None, change=True)

        self.place.refresh_from_db()
        self.assertEqual(self.place.photo_urls, initial_photo_urls)