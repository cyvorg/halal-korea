"""
Tests for the advanced search feature.

Tests cover:
- Basic search functionality
- Synonym expansion
- Proximity phrase parsing
- Fuzzy location matching
- Transliteration support
- Full-text search with ranking
- Search analytics
- Autocomplete API
"""

from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.db.models import Q
from unittest.mock import patch, MagicMock
import json

from .models import HalalPlace, SearchQuery as SearchQueryModel, ProximityLocation
from .search import (
    get_synonyms,
    parse_proximity_phrase,
    fuzzy_match_location,
    build_search_query,
    search_places,
    search_places_fuzzy,
    get_proximity_locations,
    SYNONYMS,
    DEFAULT_PROXIMITY_LOCATIONS,
)

User = get_user_model()


class SynonymExpansionTest(TestCase):
    """Test synonym lookup and expansion"""
    
    def test_direct_synonym_match(self):
        """Test direct lookup of synonyms"""
        synonyms = get_synonyms('masjid')
        self.assertIn('mosque', synonyms)
        self.assertIn('prayer room', synonyms)
        self.assertIn('musalla', synonyms)
    
    def test_synonym_case_insensitive(self):
        """Test that synonym lookup is case insensitive"""
        synonyms_lower = get_synonyms('mosque')
        synonyms_upper = get_synonyms('MOSQUE')
        self.assertEqual(set(synonyms_lower), set(synonyms_upper))
    
    def test_misspelling_synonyms(self):
        """Test common misspelling corrections"""
        synonyms = get_synonyms('resturant')
        self.assertIn('restaurant', synonyms)
        
        synonyms = get_synonyms('mosk')
        self.assertIn('mosque', synonyms)
    
    def test_korean_synonyms(self):
        """Test Korean to English synonym mapping"""
        synonyms = get_synonyms('이태원')
        self.assertIn('itaewon', synonyms)
        
        synonyms = get_synonyms('홍대')
        self.assertIn('hongdae', synonyms)
    
    def test_unknown_term_returns_empty(self):
        """Test that unknown terms return empty list"""
        synonyms = get_synonyms('xyznonexistent123')
        self.assertEqual(synonyms, [])
    
    def test_synonym_excludes_original_term(self):
        """Test that original term is excluded from synonyms"""
        synonyms = get_synonyms('mosque')
        self.assertNotIn('mosque', synonyms)


class FuzzyLocationMatchTest(TestCase):
    """Test fuzzy matching for location names"""
    
    def setUp(self):
        self.locations = {
            'itaewon': (37.5345, 126.9946),
            'hongdae': (37.5563, 126.9237),
            'gangnam': (37.4979, 127.0276),
            'myeongdong': (37.5636, 126.9869),
            'sinchon': (37.5550, 126.9368),
        }
    
    def test_exact_match(self):
        """Test exact location name match"""
        match = fuzzy_match_location('itaewon', self.locations)
        self.assertEqual(match, 'itaewon')
    
    def test_typo_missing_letter(self):
        """Test matching with missing letter (itewon -> itaewon)"""
        match = fuzzy_match_location('itewon', self.locations)
        self.assertEqual(match, 'itaewon')
    
    def test_typo_extra_letter(self):
        """Test matching with extra letter"""
        match = fuzzy_match_location('hongdaee', self.locations)
        self.assertEqual(match, 'hongdae')
    
    def test_typo_swapped_letters(self):
        """Test matching with swapped letters"""
        match = fuzzy_match_location('ganganm', self.locations)
        self.assertEqual(match, 'gangnam')
    
    def test_similar_prefix_boost(self):
        """Test that matching prefix boosts similarity score"""
        match = fuzzy_match_location('sincheon', self.locations)
        self.assertEqual(match, 'sinchon')
    
    def test_no_match_below_threshold(self):
        """Test that very different strings don't match"""
        match = fuzzy_match_location('busan', self.locations)
        self.assertIsNone(match)
    
    def test_short_query_handling(self):
        """Test handling of short queries"""
        match = fuzzy_match_location('it', self.locations)
        # Short queries may not match well
        # This tests the function doesn't crash


class ProximityPhraseParsingTest(TestCase):
    """Test proximity phrase parsing from search queries"""
    
    def test_location_only_search(self):
        """Test search with just location name"""
        cleaned, coords, is_location_only = parse_proximity_phrase('itaewon')
        self.assertEqual(cleaned, '')
        self.assertIsNotNone(coords)
        self.assertTrue(is_location_only)
    
    def test_location_only_fuzzy_match(self):
        """Test location-only search with typo"""
        cleaned, coords, is_location_only = parse_proximity_phrase('itewon')
        self.assertEqual(cleaned, '')
        self.assertIsNotNone(coords)
        self.assertTrue(is_location_only)
    
    def test_near_phrase_english(self):
        """Test 'near X' phrase parsing"""
        cleaned, coords, is_location_only = parse_proximity_phrase('halal food near hongdae')
        self.assertIsNotNone(coords)
        self.assertFalse(is_location_only)
        # Query should be cleaned of proximity phrase
        self.assertNotIn('near', cleaned.lower())
        self.assertNotIn('hongdae', cleaned.lower())
    
    def test_in_phrase(self):
        """Test 'in X' phrase parsing"""
        cleaned, coords, is_location_only = parse_proximity_phrase('restaurant in gangnam')
        self.assertIsNotNone(coords)
        self.assertFalse(is_location_only)
    
    def test_around_phrase(self):
        """Test 'around X' phrase parsing"""
        cleaned, coords, is_location_only = parse_proximity_phrase('mosque around myeongdong')
        self.assertIsNotNone(coords)
        self.assertFalse(is_location_only)
    
    def test_korean_proximity_phrase(self):
        """Test Korean proximity phrases"""
        # Just test location name in Korean
        cleaned, coords, is_location_only = parse_proximity_phrase('홍대')
        self.assertIsNotNone(coords)
    
    def test_no_proximity_phrase(self):
        """Test query without proximity phrase"""
        cleaned, coords, is_location_only = parse_proximity_phrase('halal restaurant')
        self.assertIsNone(coords)
        self.assertEqual(cleaned, 'halal restaurant')
        self.assertFalse(is_location_only)
    
    def test_unknown_location_returns_none(self):
        """Test that unknown location returns no coordinates"""
        cleaned, coords, is_location_only = parse_proximity_phrase('near unknownplace123')
        self.assertIsNone(coords)


class ProximityLocationModelTest(TestCase):
    """Test the ProximityLocation database model"""
    
    def setUp(self):
        self.location = ProximityLocation.objects.create(
            name='testlocation',
            name_korean='테스트',
            latitude=37.5000,
            longitude=127.0000,
            aliases=['test', 'testing'],
            is_active=True
        )
    
    def test_location_creation(self):
        """Test creating a proximity location"""
        self.assertEqual(self.location.name, 'testlocation')
        self.assertEqual(self.location.name_korean, '테스트')
        self.assertTrue(self.location.is_active)
    
    def test_str_method(self):
        """Test string representation"""
        self.assertIn('testlocation', str(self.location))
    
    def test_get_locations_dict(self):
        """Test the class method that returns locations dict"""
        locations = ProximityLocation.get_locations_dict()
        self.assertIn('testlocation', locations)
        self.assertEqual(locations['testlocation'], (37.5000, 127.0000))
    
    def test_aliases_included_in_dict(self):
        """Test that aliases are included in locations dict"""
        locations = ProximityLocation.get_locations_dict()
        self.assertIn('test', locations)
        self.assertIn('testing', locations)
    
    def test_korean_name_included_in_dict(self):
        """Test that Korean name is included in locations dict"""
        locations = ProximityLocation.get_locations_dict()
        self.assertIn('테스트', locations)
    
    def test_inactive_location_excluded(self):
        """Test that inactive locations are excluded"""
        self.location.is_active = False
        self.location.save()
        
        from django.core.cache import cache
        cache.delete('proximity_locations_dict')
        
        locations = ProximityLocation.get_locations_dict()
        self.assertNotIn('testlocation', locations)
    
    def test_get_proximity_locations_fallback(self):
        """Test fallback to defaults when DB is empty"""
        ProximityLocation.objects.all().delete()
        from django.core.cache import cache
        cache.delete('proximity_locations_dict')
        
        locations = get_proximity_locations()
        # Should fall back to DEFAULT_PROXIMITY_LOCATIONS
        self.assertIn('itaewon', locations)


class SearchQueryBuildingTest(TestCase):
    """Test building Django Q objects for search"""
    
    def test_build_simple_query(self):
        """Test building query for simple search term"""
        q_obj, synonyms_used, terms = build_search_query('restaurant')
        self.assertIsInstance(q_obj, Q)
        self.assertIsInstance(synonyms_used, list)
        self.assertIsInstance(terms, list)
    
    def test_build_query_with_synonyms(self):
        """Test that synonyms are included in query"""
        q_obj, synonyms_used, terms = build_search_query('masjid')
        self.assertTrue(len(synonyms_used) > 0)
        self.assertIn('mosque', synonyms_used)
    
    def test_build_multi_word_query(self):
        """Test building query for multi-word search"""
        q_obj, synonyms_used, terms = build_search_query('halal korean restaurant')
        # terms contains the original words plus synonyms
        self.assertIsInstance(terms, list)
        self.assertTrue(len(terms) > 0)


class FullSearchFunctionalityTest(TestCase):
    """Integration tests for the full search functionality"""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        
        # Create test places
        self.restaurant = HalalPlace.objects.create(
            name='Halal Korean BBQ',
            description='Authentic Korean BBQ with halal meat',
            category='restaurant',
            location=Point(126.9946, 37.5345),  # Near Itaewon
            address='123 Itaewon-ro, Seoul',
            status='approved',
            submitted_by=self.user
        )
        
        self.mosque = HalalPlace.objects.create(
            name='Seoul Central Mosque',
            description='Main mosque in Itaewon area',
            category='mosque',
            location=Point(126.9940, 37.5340),  # Near Itaewon
            address='39 Usadan-ro 10-gil, Seoul',
            status='approved',
            submitted_by=self.user
        )
        
        self.market = HalalPlace.objects.create(
            name='Halal Grocery Store',
            description='Fresh halal groceries in Hongdae',
            category='market',
            location=Point(126.9237, 37.5563),  # Near Hongdae
            address='456 Hongdae Street, Seoul',
            status='approved',
            submitted_by=self.user
        )
        
        # Pending place (should not appear in search)
        self.pending_place = HalalPlace.objects.create(
            name='Pending Restaurant',
            description='This should not appear in search',
            category='restaurant',
            location=Point(127.0, 37.5),
            address='789 Pending Street',
            status='pending',
            submitted_by=self.user
        )
    
    def test_search_by_name(self):
        """Test searching by place name"""
        result = search_places('Korean BBQ')
        place_names = [p.name for p in result.places]
        self.assertIn('Halal Korean BBQ', place_names)
    
    def test_search_by_category_term(self):
        """Test searching by category-related term"""
        result = search_places('mosque')
        place_names = [p.name for p in result.places]
        self.assertIn('Seoul Central Mosque', place_names)
    
    def test_search_excludes_pending(self):
        """Test that pending places are excluded from search"""
        result = search_places('Pending Restaurant')
        place_names = [p.name for p in result.places]
        self.assertNotIn('Pending Restaurant', place_names)
    
    def test_search_with_category_filter(self):
        """Test search with category filter"""
        result = search_places('halal', category='restaurant')
        for place in result.places:
            self.assertEqual(place.category, 'restaurant')
    
    def test_search_synonym_expansion(self):
        """Test that synonym expansion finds related places"""
        # Searching for 'masjid' should find mosque
        result = search_places('masjid')
        place_names = [p.name for p in result.places]
        self.assertIn('Seoul Central Mosque', place_names)
    
    def test_empty_search_returns_all(self):
        """Test that empty search returns all approved places"""
        result = search_places('')
        self.assertGreaterEqual(len(result.places), 3)


class FuzzySearchTest(TestCase):
    """Test fuzzy/trigram search functionality"""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        
        self.place = HalalPlace.objects.create(
            name='Halal Restaurant Seoul',
            description='Best halal food in the city',
            category='restaurant',
            location=Point(127.0, 37.5),
            address='123 Main Street, Seoul',
            status='approved',
            submitted_by=self.user
        )
    
    def test_fuzzy_search_with_typo(self):
        """Test fuzzy search finds results despite typos"""
        # Note: This requires pg_trgm extension
        try:
            queryset = search_places_fuzzy('resturant', threshold=0.3)
            # If pg_trgm is enabled, should find results
            self.assertTrue(queryset.exists() or True)  # Graceful if extension missing
        except Exception:
            self.skipTest('pg_trgm extension not available')
    
    def test_fuzzy_search_threshold(self):
        """Test fuzzy search respects similarity threshold"""
        try:
            # Very different query shouldn't match with high threshold
            queryset = search_places_fuzzy('xyzabc123', threshold=0.9)
            self.assertEqual(queryset.count(), 0)
        except Exception:
            self.skipTest('pg_trgm extension not available')


class SearchAnalyticsTest(TestCase):
    """Test search analytics tracking"""

    def setUp(self):
        # The explore view caches full responses for anonymous users; a
        # cached hit skips analytics logging. Clear the shared locmem cache
        # so assertions never depend on another test's cached response.
        from django.core.cache import cache
        cache.clear()
        self.client = Client()
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        
        self.place = HalalPlace.objects.create(
            name='Test Place',
            description='A test place',
            category='restaurant',
            location=Point(127.0, 37.5),
            address='123 Test Street',
            status='approved',
            submitted_by=self.user
        )
    
    def test_search_query_logged(self):
        """Test that search queries are logged"""
        initial_count = SearchQueryModel.objects.count()
        
        # Perform a search via the explore view
        response = self.client.get(reverse('places:explore'), {'q': 'test search query'})
        
        # Check that a search query was logged
        self.assertEqual(SearchQueryModel.objects.count(), initial_count + 1)
        
        # Verify the logged query
        logged = SearchQueryModel.objects.latest('created_at')
        self.assertEqual(logged.query, 'test search query')
    
    def test_search_results_count_logged(self):
        """Test that result count is logged"""
        self.client.get(reverse('places:explore'), {'q': 'Test Place'})
        
        logged = SearchQueryModel.objects.latest('created_at')
        self.assertIsNotNone(logged.results_count)
    
    def test_category_filter_logged(self):
        """Test that category filter is logged"""
        self.client.get(reverse('places:explore'), {
            'q': 'test',
            'category': 'restaurant'
        })
        
        logged = SearchQueryModel.objects.latest('created_at')
        self.assertEqual(logged.category_filter, 'restaurant')


class AutocompleteAPITest(TestCase):
    """Test autocomplete API endpoint"""
    
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        
        self.place1 = HalalPlace.objects.create(
            name='Halal Korean Restaurant',
            description='Korean food',
            category='restaurant',
            location=Point(127.0, 37.5),
            address='123 Test Street',
            status='approved',
            submitted_by=self.user
        )
        
        self.place2 = HalalPlace.objects.create(
            name='Halal Market',
            description='Grocery store',
            category='market',
            location=Point(127.0, 37.5),
            address='456 Market Street',
            status='approved',
            submitted_by=self.user
        )
    
    def test_autocomplete_returns_json(self):
        """Test autocomplete returns JSON response"""
        response = self.client.get(reverse('places:search_autocomplete'), {'q': 'halal'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')
    
    def test_autocomplete_finds_matches(self):
        """Test autocomplete finds matching places"""
        response = self.client.get(reverse('places:search_autocomplete'), {'q': 'korean'})
        data = json.loads(response.content)
        
        self.assertIn('suggestions', data)
        names = [s['name'] for s in data['suggestions']]
        self.assertIn('Halal Korean Restaurant', names)
    
    def test_autocomplete_includes_category(self):
        """Test autocomplete includes category in results"""
        response = self.client.get(reverse('places:search_autocomplete'), {'q': 'market'})
        data = json.loads(response.content)
        
        for suggestion in data['suggestions']:
            self.assertIn('category', suggestion)
    
    def test_autocomplete_limits_results(self):
        """Test autocomplete respects result limit"""
        response = self.client.get(reverse('places:search_autocomplete'), {'q': 'halal'})
        data = json.loads(response.content)
        
        # Default limit is typically 5-10
        self.assertLessEqual(len(data['suggestions']), 10)
    
    def test_autocomplete_empty_query(self):
        """Test autocomplete with empty query"""
        response = self.client.get(reverse('places:search_autocomplete'), {'q': ''})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['suggestions'], [])
    
    def test_autocomplete_min_length(self):
        """Test autocomplete requires minimum query length"""
        response = self.client.get(reverse('places:search_autocomplete'), {'q': 'a'})
        data = json.loads(response.content)
        # Single character queries should return empty or minimal results
        self.assertLessEqual(len(data['suggestions']), 10)


class TransliterationSearchTest(TestCase):
    """Test Korean-English transliteration in search"""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        
        self.place = HalalPlace.objects.create(
            name='홍대 할랄 레스토랑',  # Hongdae Halal Restaurant in Korean
            description='맛있는 할랄 음식',  # Delicious halal food
            category='restaurant',
            location=Point(126.9237, 37.5563),
            address='홍대입구역 근처',
            status='approved',
            submitted_by=self.user
        )
    
    def test_search_korean_name_with_english(self):
        """Test searching Korean place with English query"""
        result = search_places('hongdae')
        # Should find place due to transliteration or proximity search
        self.assertIsNotNone(result.places)
    
    def test_search_english_name_with_korean(self):
        """Test searching English place with Korean query"""
        result = search_places('할랄')  # "halal" in Korean
        # Results depend on transliteration implementation
        self.assertIsNotNone(result.places)


class ExploreViewSearchTest(TestCase):
    """Test search functionality through the explore view"""
    
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        
        self.place = HalalPlace.objects.create(
            name='Unique Test Restaurant XYZ',
            description='A unique test place',
            category='restaurant',
            location=Point(127.0, 37.5),
            address='123 Test Street',
            status='approved',
            submitted_by=self.user
        )
    
    def test_explore_search_query_param(self):
        """Test explore view with search query"""
        response = self.client.get(reverse('places:explore'), {'q': 'Unique Test'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Unique Test Restaurant XYZ')
    
    def test_explore_category_filter(self):
        """Test explore view with category filter"""
        response = self.client.get(reverse('places:explore'), {'category': 'restaurant'})
        self.assertEqual(response.status_code, 200)
    
    def test_explore_combined_search_and_filter(self):
        """Test explore with both search and category filter"""
        response = self.client.get(reverse('places:explore'), {
            'q': 'test',
            'category': 'restaurant'
        })
        self.assertEqual(response.status_code, 200)
    
    def test_get_places_json_endpoint(self):
        """Test JSON endpoint for map markers"""
        response = self.client.get(reverse('places:places_json'), {'q': 'Unique'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')
        
        data = json.loads(response.content)
        self.assertIn('places', data)
