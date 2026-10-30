from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from .models import HalalPlace
from .forms import HalalPlaceForm
from reviews.models import Review
import json

User = get_user_model()

class HalalPlaceModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.place = HalalPlace.objects.create(
            name='Test Restaurant',
            description='A test halal restaurant',
            category='restaurant',
            location=Point(126.9780, 37.5665),  # Seoul coordinates
            address='123 Test Street, Seoul',
            phone_number='02-123-4567',
            website='https://test.com',
            status='approved',
            submitted_by=self.user
        )

    def test_place_creation(self):
        """Test that a halal place can be created"""
        self.assertEqual(self.place.name, 'Test Restaurant')
        self.assertEqual(self.place.category, 'restaurant')
        self.assertEqual(self.place.status, 'approved')
        self.assertEqual(self.place.submitted_by, self.user)

    def test_place_str_method(self):
        """Test the string representation of a place"""
        self.assertEqual(str(self.place), 'Test Restaurant')

class HalalPlaceViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123',
            email_verified=True  # Required for submit_place view
        )
        self.place = HalalPlace.objects.create(
            name='Test Restaurant',
            description='A test halal restaurant',
            category='restaurant',
            location=Point(126.9780, 37.5665),
            address='123 Test Street, Seoul',
            status='approved',
            submitted_by=self.user
        )
        self.review = Review.objects.create(
            place=self.place,
            user=self.user,
            rating=5,
            comment='Great place!'
        )

    def test_home_page(self):
        """Test the home page view"""
        response = self.client.get(reverse('places:home'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'places/home.html')
        self.assertContains(response, 'Test Restaurant')

    def test_explore_page(self):
        """Test the explore page view"""
        response = self.client.get(reverse('places:explore'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'places/explore.html')
        self.assertContains(response, 'Test Restaurant')

    def test_place_detail_page(self):
        """Test the place detail page view"""
        response = self.client.get(reverse('places:place_detail', args=[self.place.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'places/place_detail.html')
        self.assertContains(response, 'Test Restaurant')
        self.assertContains(response, 'Great place!')

    def test_submit_place_requires_login(self):
        """Test that submitting a place requires login"""
        response = self.client.get(reverse('places:submit_place'))
        self.assertEqual(response.status_code, 302)  # Redirects to login

    def test_submit_place_authenticated(self):
        """Test submitting a place when authenticated"""
        self.client.login(username='testuser', password='testpass123')
        response = self.client.get(reverse('places:submit_place'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'places/submit_place.html')

    def test_about_page(self):
        """Test the about page view"""
        response = self.client.get(reverse('places:about'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'places/about.html')
        self.assertContains(response, '1')  # Total places count

    def test_set_location(self):
        """Test setting user location"""
        data = {
            'latitude': 37.5665,
            'longitude': 126.9780,
            'city': 'Seoul'
        }
        response = self.client.post(
            reverse('places:set_location'),
            data=json.dumps(data),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        response_data = json.loads(response.content)
        self.assertTrue(response_data['success'])

class HalalPlaceFormTest(TestCase):
    def setUp(self):
        self.form_data = {
            'name': 'New Restaurant',
            'description': 'A new halal restaurant',
            'category': 'restaurant',
            'address': '456 New Street, Seoul',
            'latitude': 37.5665,
            'longitude': 126.9780,
        }

    def test_form_valid(self):
        """Test form validation with valid data"""
        form = HalalPlaceForm(data=self.form_data)
        self.assertTrue(form.is_valid())

    def test_form_invalid(self):
        """Test form validation with invalid data"""
        form_data = self.form_data.copy()
        form_data['name'] = ''  # Empty name should be invalid
        form = HalalPlaceForm(data=form_data)
        self.assertFalse(form.is_valid())
        self.assertIn('name', form.errors)

    def test_form_category_choices(self):
        """Test form category choices"""
        form = HalalPlaceForm()
        self.assertIn(('restaurant', 'Restaurant'), form.fields['category'].choices)
        self.assertIn(('market', 'Market'), form.fields['category'].choices)
        self.assertIn(('mosque', 'Mosque'), form.fields['category'].choices)
        self.assertIn(('prayer_room', 'Prayer Space'), form.fields['category'].choices)
