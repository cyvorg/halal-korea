import io
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from places.models import HalalPlace, PlaceImageSuggestion

MEDIA = tempfile.mkdtemp()


@override_settings(MEDIA_ROOT=MEDIA)
class ImageSuggestionBulkApprovalTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("imgsug", "imgsug@example.com", "pw12345!x")
        self.admin = User.objects.create_superuser("imgadm", "imgadm@example.com", "pw12345!x")
        self.place = HalalPlace.objects.create(
            name="P", address="a", location=Point(127, 37), status="approved"
        )

    def _suggest(self, n):
        buf = io.BytesIO()
        Image.new("RGB", (30, 30), "red").save(buf, "PNG")
        for i in range(n):
            PlaceImageSuggestion.objects.create(
                place=self.place,
                suggested_by=self.user,
                original_image=SimpleUploadedFile(f"o{i}.png", buf.getvalue(), "image/png"),
                image=SimpleUploadedFile(f"wm_{i}.png", buf.getvalue(), "image/png"),
            )

    def _approve(self, action="approve_image_suggestions"):
        self.client.force_login(self.admin)
        ids = list(PlaceImageSuggestion.objects.values_list("pk", flat=True))
        return self.client.post(
            reverse("admin:places_placeimagesuggestion_changelist"),
            {"action": action, "_selected_action": ids},
            follow=True,
        )

    def test_bulk_approving_several_images_for_one_place_keeps_all(self):
        self._suggest(3)
        self._approve()
        self.place.refresh_from_db()
        self.assertEqual(len(self.place.photo_urls), 3)
        self.assertEqual(len(set(self.place.photo_urls)), 3)
        self.assertEqual(
            set(PlaceImageSuggestion.objects.values_list("status", flat=True)), {"approved"}
        )

    def test_approving_twice_does_not_duplicate(self):
        self._suggest(2)
        self._approve()
        self._approve()
        self.place.refresh_from_db()
        self.assertEqual(len(self.place.photo_urls), 2)

    def test_existing_photos_are_kept(self):
        self.place.photo_urls = ["https://example.com/existing.jpg"]
        self.place.save()
        self._suggest(2)
        self._approve()
        self.place.refresh_from_db()
        self.assertEqual(len(self.place.photo_urls), 3)
        self.assertEqual(self.place.photo_urls[0], "https://example.com/existing.jpg")


class EditSuggestionBulkApprovalTests(TestCase):
    def test_bulk_approving_several_field_edits_for_one_place_keeps_all(self):
        from places.models import PlaceEditSuggestion

        User = get_user_model()
        user = User.objects.create_user("edsug", "edsug@example.com", "pw12345!x")
        admin = User.objects.create_superuser("edadm", "edadm@example.com", "pw12345!x")
        place = HalalPlace.objects.create(
            name="Old Name", address="Old Addr", location=Point(127, 37), status="approved"
        )
        for field, value in (("name", "New Name"), ("address", "New Addr"), ("phone_number", "010-1")):
            PlaceEditSuggestion.objects.create(
                place=place, suggested_by=user, field_name=field,
                current_value="", suggested_value=value, reason="r",
            )
        self.client.force_login(admin)
        ids = list(PlaceEditSuggestion.objects.values_list("pk", flat=True))
        self.client.post(
            reverse("admin:places_placeeditsuggestion_changelist"),
            {"action": "approve_suggestions", "_selected_action": ids},
            follow=True,
        )
        place.refresh_from_db()
        self.assertEqual(
            (place.name, place.address, place.phone_number),
            ("New Name", "New Addr", "010-1"),
        )
