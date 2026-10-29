from django.contrib import admin
from django.utils.html import format_html
from django.utils import timezone
from django.contrib import messages
from django.contrib.gis.geos import Point
from django.db import transaction
from django.db.models import Count, Q
from django.urls import path
from django.http import HttpResponse, JsonResponse
from django.template.response import TemplateResponse
from django.shortcuts import get_object_or_404
from django.forms import ModelForm, CharField, Textarea
from django.core.exceptions import ValidationError
import json
import logging

from .models import (
    HalalPlace,
    PlaceEditSuggestion,
    PlaceImageSuggestion,
    SearchQuery,
    ProximityLocation,
    BusinessHours,
    TimeSlot,
    BusinessHoursSuggestion,
)
from .export_utils import PlaceExporter

logger = logging.getLogger(__name__)


class PlaceEditSuggestionAdminForm(ModelForm):
    """Custom form for PlaceEditSuggestion admin with helpful guidance"""

    class Meta:
        model = PlaceEditSuggestion
        fields = "__all__"
        help_texts = {
            "status": 'Change to "Approved" to automatically apply this suggestion to the place. Change to "Rejected" to dismiss it.',
            "admin_notes": "Optional notes for internal use. Will be auto-populated if automatic application fails.",
        }


class PlaceImageSuggestionAdminForm(ModelForm):
    """Custom form for PlaceImageSuggestion admin with helpful guidance"""

    class Meta:
        model = PlaceImageSuggestion
        fields = "__all__"
        help_texts = {
            "status": 'Change to "Approved" to automatically add this image to the place gallery. Change to "Rejected" to dismiss it.',
            "admin_notes": "Optional notes for internal use. Will be auto-populated if automatic application fails.",
        }


# Business Hours Admin - Separate admin for better UX
class TimeSlotInline(admin.TabularInline):
    """Inline for editing time slots within BusinessHours"""

    model = TimeSlot
    extra = 7  # One for each day of the week
    min_num = 0

    fields = ("day_of_week", "is_closed", "open_time", "close_time")
    ordering = ("day_of_week", "open_time")

    def get_queryset(self, request):
        return super().get_queryset(request).order_by("day_of_week", "open_time")


@admin.register(BusinessHours)
class BusinessHoursAdmin(admin.ModelAdmin):
    """Admin for managing business hours with time slots"""

    list_display = ("place", "is_24_hours", "time_slots_summary", "updated_at")
    list_filter = ("is_24_hours",)
    search_fields = ("place__name",)
    readonly_fields = ("created_at", "updated_at")
    inlines = [TimeSlotInline]

    fieldsets = (
        ("Place", {"fields": ("place",)}),
        ("Settings", {"fields": ("is_24_hours", "notes")}),
        (
            "Metadata",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )

    def time_slots_summary(self, obj):
        """Show summary of time slots"""
        slots = obj.time_slots.all()
        if not slots:
            return "No time slots set"

        count_by_day = {}
        for slot in slots:
            day = slot.get_day_of_week_display()
            count_by_day[day] = count_by_day.get(day, 0) + 1

        summary = ", ".join([f"{day}: {count}" for day, count in count_by_day.items()])
        return summary or "No time slots"

    time_slots_summary.short_description = "Time Slots"


class BusinessHoursInline(admin.StackedInline):
    """Inline for viewing/creating business hours link within HalalPlace"""

    model = BusinessHours
    extra = 0
    max_num = 1
    can_delete = False
    show_change_link = True  # This allows clicking through to edit with time slots

    fields = ("is_24_hours", "notes")
    readonly_fields = (
        "is_24_hours",
        "notes",
    )  # Make read-only in inline, edit in separate admin

    def has_add_permission(self, request, obj=None):
        # Allow adding if place doesn't have business hours yet
        if obj and hasattr(obj, "business_hours"):
            return False
        return True


# Custom admin filters
class StatusFilter(admin.SimpleListFilter):
    title = "Status"
    parameter_name = "status"

    def lookups(self, request, model_admin):
        return (
            ("pending", "Pending Review"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
            ("archived", "Archived"),
            ("with_suggestions", "Has Pending Suggestions"),
            ("popular", "Popular (Has Reviews)"),
        )

    def queryset(self, request, queryset):
        if self.value() == "with_suggestions":
            return queryset.filter(edit_suggestions__status="pending").distinct()
        elif self.value() == "popular":
            return queryset.annotate(review_count=Count("reviews")).filter(
                review_count__gt=0
            )
        elif self.value():
            return queryset.filter(status=self.value())
        return queryset


class CategoryFilter(admin.SimpleListFilter):
    title = "Category"
    parameter_name = "category"

    def lookups(self, request, model_admin):
        return HalalPlace.CATEGORY_CHOICES + [
            ("with_images", "Has Images"),
            ("no_images", "No Images"),
        ]

    def queryset(self, request, queryset):
        if self.value() == "with_images":
            return queryset.exclude(Q(photo_urls__isnull=True) | Q(photo_urls=[]))
        elif self.value() == "no_images":
            return queryset.filter(Q(photo_urls__isnull=True) | Q(photo_urls=[]))
        elif self.value():
            return queryset.filter(category=self.value())
        return queryset


# Custom form for HalalPlace with enhanced image management
class HalalPlaceAdminForm(ModelForm):
    image_management = CharField(
        required=False,
        widget=Textarea(
            attrs={"style": "display: none;", "id": "image-management-field"}
        ),
        help_text="Advanced image management interface",
    )

    class Meta:
        model = HalalPlace
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            # Populate image management field with current photos
            if self.instance.photo_urls:
                self.fields["image_management"].initial = json.dumps(
                    self.instance.photo_urls
                )

    def clean_photo_urls(self):
        photo_urls = self.cleaned_data.get("photo_urls")
        if photo_urls:
            # Validate each URL
            for url in photo_urls:
                if url and not url.startswith(("http://", "https://", "/")):
                    raise ValidationError(f"Invalid URL format: {url}")
        return photo_urls


@admin.register(HalalPlace)
class HalalPlaceAdmin(admin.ModelAdmin):
    form = HalalPlaceAdminForm
    list_display = (
        "name_with_link",
        "category_icon",
        "status",
        "location_display",
        "image_count",
        "suggestion_count",
        "submitted_by",
        "created_at",
    )
    list_filter = (
        StatusFilter,
        CategoryFilter,
        "submitted_by",
        "created_at",
        "updated_at",
    )
    search_fields = ("name", "description", "address", "submitted_by__username")
    readonly_fields = (
        "created_at",
        "updated_at",
        "image_preview_grid",
        "suggestion_summary",
        "location_map",
        "status_badge",
    )
    list_editable = ("status",)
    list_per_page = 25
    ordering = ("-created_at",)

    actions = [
        "bulk_approve",
        "bulk_archive",
        "bulk_reject",
        "bulk_set_pending",
        "export_selected_csv",
        "export_selected_json",
        "export_selected_excel",
        "export_selected_geojson",
        "validate_images",
        "generate_stats",
    ]

    fieldsets = (
        (
            "Basic Information",
            {"fields": ("name", "description", "category", "status", "status_badge")},
        ),
        (
            "Location",
            {"fields": ("address", "location", "location_map"), "classes": ("wide",)},
        ),
        (
            "Contact Information",
            {"fields": ("phone_number", "website"), "classes": ("collapse",)},
        ),
        (
            "Map Links",
            {
                "fields": ("google_map_link", "kakao_map_link", "naver_map_link"),
                "classes": ("collapse",),
            },
        ),
        (
            "Images",
            {
                "fields": ("photo_urls", "image_preview_grid", "image_management"),
                "classes": ("wide",),
            },
        ),
        (
            "Metadata",
            {
                "fields": ("submitted_by", "created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
        ("Suggestions", {"fields": ("suggestion_summary",), "classes": ("collapse",)}),
        (
            "Temporary Closure",
            {
                "fields": ("temporary_closure_until", "temporary_closure_reason"),
                "classes": ("collapse",),
                "description": 'Temporary closure settings. For regular business hours, click "Business Hours" below or edit in the separate Business Hours admin.',
            },
        ),
    )

    inlines = [BusinessHoursInline]

    class Media:
        css = {"all": ("admin/css/enhanced_admin.css",)}
        js = ("admin/js/enhanced_admin.js", "admin/js/image_management.js")

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "image-manager/<int:place_id>/",
                self.admin_site.admin_view(self.image_manager_view),
                name="places_halalplace_image_manager",
            ),
            path(
                "bulk-stats/",
                self.admin_site.admin_view(self.bulk_stats_view),
                name="places_halalplace_bulk_stats",
            ),
        ]
        return custom_urls + urls

    # Custom display methods
    def name_with_link(self, obj):
        url = f"/admin/places/halalplace/{obj.pk}/change/"
        return format_html(
            '<a href="{}" style="font-weight: bold; color: #0066cc;">{}</a>',
            url,
            obj.name,
        )

    name_with_link.short_description = "Name"
    name_with_link.admin_order_field = "name"

    def category_icon(self, obj):
        icons = {"restaurant": "🍽️", "market": "🛒", "mosque": "🕌", "prayer_room": "🤲"}
        icon = icons.get(obj.category, "📍")
        return format_html(
            '<span style="font-size: 18px; margin-right: 5px;">{}</span>{}',
            icon,
            obj.get_category_display(),
        )

    category_icon.short_description = "Category"
    category_icon.admin_order_field = "category"

    def status_badge(self, obj):
        colors = {
            "pending": "#fbbf24",
            "approved": "#10b981",
            "rejected": "#ef4444",
            "archived": "#6b7280",
        }
        color = colors.get(obj.status, "#6b7280")
        return format_html(
            '<span style="background-color: {}; color: white; padding: 4px 8px; border-radius: 12px; font-size: 11px; font-weight: bold;">{}</span>',
            color,
            obj.get_status_display().upper(),
        )

    status_badge.short_description = "Status"
    status_badge.admin_order_field = "status"

    def location_display(self, obj):
        if obj.location:
            lat, lng = obj.location.y, obj.location.x
            address_display = (
                obj.address[:30] + "..." if len(obj.address) > 30 else obj.address
            )
            return format_html(
                '<div style="font-size: 11px;"><strong>📍</strong> {}, {}<br/><span style="color: #666;">{}</span></div>',
                round(lat, 4),
                round(lng, 4),
                address_display,
            )
        return format_html('<span style="color: #999;">No location</span>')

    location_display.short_description = "Location"

    def image_count(self, obj):
        count = len(obj.photo_urls) if obj.photo_urls else 0
        if count > 0:
            return format_html(
                '<div style="text-align: center;"><strong style="color: #059669; font-size: 16px;">{}</strong><br/><span style="font-size: 10px; color: #666;">images</span></div>',
                count,
            )
        return format_html('<span style="color: #999;">No images</span>')

    image_count.short_description = "Images"

    def suggestion_count(self, obj):
        pending = obj.edit_suggestions.filter(status="pending").count()
        approved = obj.edit_suggestions.filter(status="approved").count()
        if pending > 0:
            return format_html(
                '<div style="text-align: center;"><strong style="color: #dc2626;">{}</strong> pending<br/><span style="font-size: 10px; color: #666;">{} approved</span></div>',
                pending,
                approved,
            )
        elif approved > 0:
            return format_html(
                '<div style="text-align: center;"><span style="color: #059669;">{} approved</span></div>',
                approved,
            )
        return format_html('<span style="color: #999;">No suggestions</span>')

    suggestion_count.short_description = "Suggestions"

    def image_preview_grid(self, obj):
        if not obj.photo_urls:
            return format_html(
                '<p style="color: #666; font-style: italic;">No images uploaded</p>'
            )

        html_parts = [
            '<div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 10px; max-width: 600px;">'
        ]

        for i, url in enumerate(obj.photo_urls[:8]):  # Show max 8 images
            html_parts.append(
                f'<div style="position: relative; border: 1px solid #ddd; border-radius: 8px; overflow: hidden;">'
                f'<img src="{url}" style="width: 100%; height: 80px; object-fit: cover;" alt="Image {i + 1}" />'
                f'<div style="position: absolute; top: 2px; left: 2px; background: rgba(0,0,0,0.7); color: white; padding: 2px 6px; border-radius: 4px; font-size: 10px;">{i + 1}</div>'
                f"</div>"
            )

        if len(obj.photo_urls) > 8:
            html_parts.append(
                f'<div style="display: flex; align-items: center; justify-content: center; background: #f3f4f6; border: 1px solid #ddd; border-radius: 8px; height: 80px; color: #666; font-size: 12px;">+{len(obj.photo_urls) - 8} more</div>'
            )

        html_parts.append("</div>")
        html_parts.append(
            f'<p style="margin-top: 10px;"><a href="/admin/places/halalplace/image-manager/{obj.pk}/" style="background: #0066cc; color: white; padding: 6px 12px; border-radius: 4px; text-decoration: none; font-size: 12px;">🖼️ Manage Images</a></p>'
        )

        return format_html("".join(html_parts))

    image_preview_grid.short_description = "Image Gallery"

    def suggestion_summary(self, obj):
        suggestions = obj.edit_suggestions.all()
        if not suggestions:
            return format_html(
                '<p style="color: #666; font-style: italic;">No suggestions</p>'
            )

        pending = suggestions.filter(status="pending")
        approved = suggestions.filter(status="approved")
        rejected = suggestions.filter(status="rejected")

        html = '<div style="font-size: 12px;">'
        if pending:
            html += f'<div style="margin-bottom: 5px;"><strong style="color: #dc2626;">⏳ {pending.count()} Pending:</strong>'
            for suggestion in pending[:3]:
                html += f"<br/>• {suggestion.get_field_name_display()} by {suggestion.suggested_by.username}"
            if pending.count() > 3:
                html += f"<br/>• ... and {pending.count() - 3} more"
            html += "</div>"

        if approved:
            html += f'<div style="color: #059669;">✅ {approved.count()} Approved</div>'
        if rejected:
            html += f'<div style="color: #dc2626;">❌ {rejected.count()} Rejected</div>'

        html += "</div>"
        return format_html(html)

    suggestion_summary.short_description = "Suggestion Summary"

    def location_map(self, obj):
        if obj.location:
            lat, lng = obj.location.y, obj.location.x
            return format_html(
                '<div style="text-align: center;">'
                '<iframe src="https://maps.google.com/maps?q={},{}&z=15&output=embed" '
                'width="300" height="200" style="border: 1px solid #ddd; border-radius: 8px;"></iframe>'
                '<p style="margin-top: 5px; font-size: 11px; color: #666;">Lat: {}, Lng: {}</p>'
                "</div>",
                lat,
                lng,
                round(lat, 6),
                round(lng, 6),
            )
        return format_html(
            '<p style="color: #666; font-style: italic;">No location set</p>'
        )

    location_map.short_description = "Map Preview"

    # Bulk action methods
    def bulk_approve(self, request, queryset):
        updated = queryset.update(status="approved")
        self.message_user(request, f"Successfully approved {updated} place(s).")

    bulk_approve.short_description = "✅ Approve selected places"

    def bulk_archive(self, request, queryset):
        updated = queryset.update(status="archived")
        self.message_user(request, f"Successfully archived {updated} place(s).")

    bulk_archive.short_description = "📦 Archive selected places"

    def bulk_reject(self, request, queryset):
        updated = queryset.update(status="rejected")
        self.message_user(request, f"Successfully rejected {updated} place(s).")

    bulk_reject.short_description = "❌ Reject selected places"

    def bulk_set_pending(self, request, queryset):
        updated = queryset.update(status="pending")
        self.message_user(request, f"Successfully set {updated} place(s) to pending.")

    bulk_set_pending.short_description = "⏳ Set to pending review"

    def export_selected_json(self, request, queryset):
        """Export selected places to JSON"""
        exporter = PlaceExporter(queryset)
        return exporter.get_http_response("json", "halal_places_export.json")

    export_selected_json.short_description = "📄 Export selected to JSON"

    def export_selected_excel(self, request, queryset):
        """Export selected places to Excel"""
        exporter = PlaceExporter(queryset)
        return exporter.get_http_response("excel", "halal_places_export.xlsx")

    export_selected_excel.short_description = "📊 Export selected to Excel"

    def export_selected_geojson(self, request, queryset):
        """Export selected places to GeoJSON"""
        exporter = PlaceExporter(queryset)
        return exporter.get_http_response("geojson", "halal_places_export.geojson")

    export_selected_geojson.short_description = "🗺️ Export selected to GeoJSON"

    def export_selected_csv(self, request, queryset):
        """Export selected places to CSV (enhanced version)"""
        exporter = PlaceExporter(queryset)
        return exporter.get_http_response("csv", "halal_places_export.csv")

    export_selected_csv.short_description = "📋 Export selected to CSV"

    def validate_images(self, request, queryset):
        validated_count = 0
        error_count = 0

        for place in queryset:
            if place.photo_urls:
                valid_urls = []
                for url in place.photo_urls:
                    try:
                        # Basic URL validation - you could add more sophisticated checks
                        if url and (
                            url.startswith(("http://", "https://"))
                            or url.startswith("/")
                        ):
                            valid_urls.append(url)
                        else:
                            error_count += 1
                    except Exception:
                        error_count += 1

                if len(valid_urls) != len(place.photo_urls):
                    place.photo_urls = valid_urls
                    place.save()
                    validated_count += 1

        if validated_count:
            self.message_user(
                request,
                f"Validated images for {validated_count} places. Removed {error_count} invalid URLs.",
            )
        else:
            self.message_user(request, "All image URLs are valid.")

    validate_images.short_description = "🔍 Validate image URLs"

    def generate_stats(self, request, queryset):
        stats = {
            "total": queryset.count(),
            "by_status": {},
            "by_category": {},
            "with_images": 0,
            "with_suggestions": 0,
        }

        for status_choice in HalalPlace.STATUS_CHOICES:
            count = queryset.filter(status=status_choice[0]).count()
            stats["by_status"][status_choice[1]] = count

        for category_choice in HalalPlace.CATEGORY_CHOICES:
            count = queryset.filter(category=category_choice[0]).count()
            stats["by_category"][category_choice[1]] = count

        stats["with_images"] = queryset.exclude(
            Q(photo_urls__isnull=True) | Q(photo_urls=[])
        ).count()
        stats["with_suggestions"] = (
            queryset.filter(edit_suggestions__status="pending").distinct().count()
        )

        messages.success(
            request,
            f"Generated stats for {stats['total']} places. Check the detailed breakdown in the response.",
        )

        # Store stats in session for the bulk stats view
        request.session["bulk_stats"] = stats
        return HttpResponse(f"""
            <h3>Statistics for Selected Places</h3>
            <p><strong>Total:</strong> {stats["total"]}</p>
            <h4>By Status:</h4>
            <ul>{"".join([f"<li>{k}: {v}</li>" for k, v in stats["by_status"].items()])}</ul>
            <h4>By Category:</h4>
            <ul>{"".join([f"<li>{k}: {v}</li>" for k, v in stats["by_category"].items()])}</ul>
            <p><strong>With Images:</strong> {stats["with_images"]}</p>
            <p><strong>With Pending Suggestions:</strong> {stats["with_suggestions"]}</p>
            <p><a href="javascript:window.close()">Close Window</a></p>
        """)

    generate_stats.short_description = "📈 Generate statistics"

    # Custom admin views
    def image_manager_view(self, request, place_id):
        place = get_object_or_404(HalalPlace, pk=place_id)

        if request.method == "POST":
            import json

            data = json.loads(request.body)

            if data.get("action") == "reorder":
                place.photo_urls = data.get("urls", [])
                place.save()
                return JsonResponse({"success": True})

            elif data.get("action") == "delete":
                url_to_delete = data.get("url")
                if url_to_delete and place.photo_urls:
                    place.photo_urls = [
                        url for url in place.photo_urls if url != url_to_delete
                    ]
                    place.save()
                    return JsonResponse({"success": True})

            elif data.get("action") == "add":
                new_url = data.get("url")
                if new_url:
                    if not place.photo_urls:
                        place.photo_urls = []
                    place.photo_urls.append(new_url)
                    place.save()
                    return JsonResponse({"success": True})

        context = {
            "place": place,
            "photo_urls": place.photo_urls or [],
            "title": f"Image Manager - {place.name}",
            "opts": self.model._meta,
        }

        return TemplateResponse(request, "admin/places/image_manager.html", context)

    def bulk_stats_view(self, request):
        context = {
            "title": "Bulk Statistics",
            "opts": self.model._meta,
            "stats": request.session.get("bulk_stats", {}),
        }
        return TemplateResponse(request, "admin/places/bulk_stats.html", context)


@admin.register(PlaceEditSuggestion)
class PlaceEditSuggestionAdmin(admin.ModelAdmin):
    form = PlaceEditSuggestionAdminForm
    list_display = (
        "place",
        "field_name",
        "suggested_by",
        "status_display",
        "created_at",
        "reviewed_by",
        "quick_actions",
    )
    list_filter = ("status", "field_name", "created_at")
    search_fields = ("place__name", "suggested_by__username", "reason")
    readonly_fields = (
        "created_at",
        "current_value_display",
        "suggested_value_display",
        "reviewed_by",
        "reviewed_at",
    )
    ordering = ("-created_at",)  # Newest first
    raw_id_fields = ("place", "suggested_by", "reviewed_by")
    fieldsets = (
        (
            "Suggestion Info",
            {"fields": ("place", "suggested_by", "field_name", "reason")},
        ),
        ("Values", {"fields": ("current_value_display", "suggested_value_display")}),
        ("Review", {"fields": ("status", "admin_notes", "reviewed_by", "reviewed_at")}),
        ("Timestamps", {"fields": ("created_at",), "classes": ("collapse",)}),
    )
    actions = ["approve_suggestions", "reject_suggestions"]

    def status_display(self, obj):
        """Display status with colored indicators"""
        if obj.status == "pending":
            return format_html(
                '<span style="color: #dc2626; font-weight: bold;">⏳ Pending</span>'
            )
        elif obj.status == "approved":
            return format_html(
                '<span style="color: #059669; font-weight: bold;">✅ Approved</span>'
            )
        elif obj.status == "rejected":
            return format_html(
                '<span style="color: #dc2626; font-weight: bold;">❌ Rejected</span>'
            )
        return obj.get_status_display()

    status_display.short_description = "Status"

    def quick_actions(self, obj):
        """Display quick action buttons for pending suggestions"""
        if obj.status == "pending":
            return format_html(
                '<div style="white-space: nowrap;">'
                '<a href="{}?status=approved" '
                'style="background: #059669; color: white; padding: 4px 8px; '
                'border-radius: 4px; text-decoration: none; margin-right: 4px; font-size: 11px;">'
                "✅ Approve</a>"
                '<a href="{}?status=rejected" '
                'style="background: #dc2626; color: white; padding: 4px 8px; '
                'border-radius: 4px; text-decoration: none; font-size: 11px;">'
                "❌ Reject</a>"
                "</div>",
                f"/admin/places/placeeditsuggestion/{obj.pk}/change/",
                f"/admin/places/placeeditsuggestion/{obj.pk}/change/",
            )
        return "-"

    quick_actions.short_description = "Quick Actions"

    def get_changeform_initial_data(self, request):
        """Pre-fill status field from query parameter for quick actions"""
        initial = super().get_changeform_initial_data(request)
        status = request.GET.get("status")
        if status in ["approved", "rejected"]:
            initial["status"] = status
        return initial

    def save_model(self, request, obj, form, change):
        """Handle individual approval when admin changes status to approved"""
        previous_status = None
        if change and obj.pk:
            # Get the previous status before saving
            previous_status = (
                PlaceEditSuggestion.objects.filter(pk=obj.pk)
                .values_list("status", flat=True)
                .first()
            )

        # Check if status is being changed to approved
        status_changed_to_approved = (
            obj.status == "approved" and previous_status != "approved"
        )

        if status_changed_to_approved:
            # Set reviewer info
            obj.reviewed_by = request.user
            obj.reviewed_at = timezone.now()

            # Try to apply the suggestion
            if self._apply_suggestion(obj, request.user):
                try:
                    messages.success(
                        request,
                        f'✅ Suggestion approved and applied successfully: {obj.get_field_name_display()} for "{obj.place.name}"',
                    )
                except Exception:
                    # Messages framework not available (e.g., in tests)
                    pass
            else:
                # If application failed, keep as pending but add admin notes
                obj.status = "pending"
                obj.admin_notes = f"Failed to apply automatically on {timezone.now().strftime('%Y-%m-%d %H:%M')}. Please review manually."
                try:
                    messages.error(
                        request,
                        f"❌ Failed to apply suggestion for {obj.get_field_name_display()}. Check admin notes for details.",
                    )
                except Exception:
                    # Messages framework not available (e.g., in tests)
                    pass
        elif obj.status == "rejected" and previous_status != "rejected":
            # Set reviewer info for rejection
            obj.reviewed_by = request.user
            obj.reviewed_at = timezone.now()
            try:
                messages.info(
                    request,
                    f'❌ Suggestion rejected: {obj.get_field_name_display()} for "{obj.place.name}"',
                )
            except Exception:
                # Messages framework not available (e.g., in tests)
                pass

        super().save_model(request, obj, form, change)

    def current_value_display(self, obj):
        if obj.current_value:
            return format_html(
                '<div style="background-color: #fff2cc; padding: 10px; border-radius: 4px;"><strong>Current:</strong><br>{}</div>',
                obj.current_value,
            )
        return "No current value"

    current_value_display.short_description = "Current Value"

    def suggested_value_display(self, obj):
        return format_html(
            '<div style="background-color: #d4edda; padding: 10px; border-radius: 4px;"><strong>Suggested:</strong><br>{}</div>',
            obj.suggested_value,
        )

    suggested_value_display.short_description = "Suggested Value"

    def approve_suggestions(self, request, queryset):
        approved_count = 0
        failed_count = 0

        for suggestion in queryset.filter(status="pending"):
            if self._apply_suggestion(suggestion, request.user):
                suggestion.status = "approved"
                suggestion.reviewed_by = request.user
                suggestion.reviewed_at = timezone.now()
                suggestion.save()
                approved_count += 1
            else:
                failed_count += 1
                # Keep the suggestion as pending but add admin notes
                suggestion.admin_notes = (
                    "Failed to apply automatically. Please review manually."
                )
                suggestion.save()

        if approved_count:
            messages.success(
                request,
                f"Successfully approved and applied {approved_count} suggestion(s)",
            )
        if failed_count:
            messages.error(
                request,
                f"{failed_count} suggestion(s) failed to apply. Check logs and admin notes for details.",
            )

    approve_suggestions.short_description = "Approve selected suggestions"

    def reject_suggestions(self, request, queryset):
        rejected_count = 0
        for suggestion in queryset.filter(status="pending"):
            suggestion.status = "rejected"
            suggestion.reviewed_by = request.user
            suggestion.reviewed_at = timezone.now()
            suggestion.save()
            rejected_count += 1

        if rejected_count:
            messages.success(
                request, f"Successfully rejected {rejected_count} suggestion(s)"
            )

    reject_suggestions.short_description = "Reject selected suggestions"

    def _apply_suggestion(self, suggestion, reviewer):
        """Apply an approved suggestion to the place"""
        try:
            import logging

            logger = logging.getLogger(__name__)

            # Reload the place: bulk actions hold one place instance per
            # suggestion, so an earlier approval would otherwise be overwritten.
            place = suggestion.place
            place.refresh_from_db()
            field_name = suggestion.field_name
            new_value = suggestion.suggested_value

            logger.info(
                f"Applying suggestion: {field_name} = '{new_value}' for place '{place.name}' (ID: {place.pk})"
            )

            # Handle location field specially (convert string to Point)
            if field_name == "location":
                # Expect format like "lat,lng" or "(lat, lng)"
                import re

                coords = re.findall(r"-?\d+\.?\d*", new_value)
                if len(coords) >= 2:
                    lat, lng = float(coords[0]), float(coords[1])
                    new_value = Point(lng, lat, srid=4326)
                    logger.info(f"Converted location string to Point: {new_value}")
                else:
                    logger.error(
                        f"Invalid location format: '{new_value}'. Expected 'lat,lng' format"
                    )
                    return False

            # Handle category field (validate against choices)
            elif field_name == "category":
                valid_categories = [choice[0] for choice in HalalPlace.CATEGORY_CHOICES]
                if new_value not in valid_categories:
                    logger.error(
                        f"Invalid category: '{new_value}'. Valid choices: {valid_categories}"
                    )
                    return False

            # Store old value for logging
            old_value = getattr(place, field_name)

            # Apply the change
            setattr(place, field_name, new_value)
            place.save()

            logger.info(
                f"Successfully applied suggestion: '{old_value}' -> '{new_value}' for {field_name}"
            )
            return True

        except Exception as e:
            import logging

            logger = logging.getLogger(__name__)
            logger.error(
                f"Error applying suggestion {suggestion.pk}: {str(e)}", exc_info=True
            )
            return False


@admin.register(PlaceImageSuggestion)
class PlaceImageSuggestionAdmin(admin.ModelAdmin):
    form = PlaceImageSuggestionAdminForm
    list_display = (
        "place",
        "suggested_by",
        "status_display",
        "created_at",
        "reviewed_by",
        "image_preview",
        "quick_actions",
    )
    list_filter = ("status", "created_at")
    search_fields = ("place__name", "suggested_by__username", "caption")
    readonly_fields = ("created_at", "image_preview", "reviewed_by", "reviewed_at")
    ordering = ("status", "-created_at")  # Pending first, then newest first
    raw_id_fields = ("place", "suggested_by", "reviewed_by")
    fieldsets = (
        ("Suggestion Info", {"fields": ("place", "suggested_by", "image", "caption")}),
        ("Preview", {"fields": ("image_preview",)}),
        ("Review", {"fields": ("status", "admin_notes", "reviewed_by", "reviewed_at")}),
        ("Timestamps", {"fields": ("created_at",), "classes": ("collapse",)}),
    )
    actions = [
        "approve_image_suggestions",
        "reject_image_suggestions",
        "reapply_watermark_action",
        "apply_watermark_to_existing",
    ]

    def status_display(self, obj):
        """Display status with colored indicators"""
        if obj.status == "pending":
            return format_html(
                '<span style="color: #dc2626; font-weight: bold;">⏳ Pending</span>'
            )
        elif obj.status == "approved":
            return format_html(
                '<span style="color: #059669; font-weight: bold;">✅ Approved</span>'
            )
        elif obj.status == "rejected":
            return format_html(
                '<span style="color: #dc2626; font-weight: bold;">❌ Rejected</span>'
            )
        return obj.get_status_display()

    status_display.short_description = "Status"

    def quick_actions(self, obj):
        """Display quick action buttons for pending suggestions"""
        if obj.status == "pending":
            return format_html(
                '<div style="white-space: nowrap;">'
                '<a href="{}?status=approved" '
                'style="background: #059669; color: white; padding: 4px 8px; '
                'border-radius: 4px; text-decoration: none; margin-right: 4px; font-size: 11px;">'
                "✅ Approve</a>"
                '<a href="{}?status=rejected" '
                'style="background: #dc2626; color: white; padding: 4px 8px; '
                'border-radius: 4px; text-decoration: none; font-size: 11px;">'
                "❌ Reject</a>"
                "</div>",
                f"/admin/places/placeimagesuggestion/{obj.pk}/change/",
                f"/admin/places/placeimagesuggestion/{obj.pk}/change/",
            )
        return "-"

    quick_actions.short_description = "Quick Actions"

    def get_changeform_initial_data(self, request):
        """Pre-fill status field from query parameter for quick actions"""
        initial = super().get_changeform_initial_data(request)
        status = request.GET.get("status")
        if status in ["approved", "rejected"]:
            initial["status"] = status
        return initial

    def save_model(self, request, obj, form, change):
        """Handle individual approval when admin changes status to approved"""
        previous_status = None
        if change and obj.pk:
            # Get the previous status before saving
            previous_status = (
                PlaceImageSuggestion.objects.filter(pk=obj.pk)
                .values_list("status", flat=True)
                .first()
            )

        # Check if status is being changed to approved
        status_changed_to_approved = (
            obj.status == "approved" and previous_status != "approved"
        )

        if status_changed_to_approved:
            # Set reviewer info
            if not obj.reviewed_by:
                obj.reviewed_by = request.user
            obj.reviewed_at = timezone.now()

            # Save first to ensure we have the updated object
            super().save_model(request, obj, form, change)

            # Try to apply the image suggestion
            if self._apply_image_suggestion(obj, request.user):
                try:
                    messages.success(
                        request,
                        f'✅ Image approved and added successfully to "{obj.place.name}" photo gallery',
                    )
                except Exception:
                    # Messages framework not available (e.g., in tests)
                    pass
            else:
                # If application failed, revert to pending and add admin notes
                obj.status = "pending"
                obj.admin_notes = f"Failed to apply automatically on {timezone.now().strftime('%Y-%m-%d %H:%M')}. Please review manually."
                obj.save(update_fields=["status", "admin_notes"])
                try:
                    messages.error(
                        request,
                        f'❌ Failed to add image to "{obj.place.name}". Check admin notes for details.',
                    )
                except Exception:
                    # Messages framework not available (e.g., in tests)
                    pass
        elif obj.status == "rejected" and previous_status != "rejected":
            # Set reviewer info for rejection
            obj.reviewed_by = request.user
            obj.reviewed_at = timezone.now()
            try:
                messages.info(
                    request, f'❌ Image suggestion rejected for "{obj.place.name}"'
                )
            except Exception:
                # Messages framework not available (e.g., in tests)
                pass
            super().save_model(request, obj, form, change)
        else:
            # Normal save for other changes
            super().save_model(request, obj, form, change)

    def image_preview(self, obj):
        if obj.image:
            return format_html(
                '<div style="text-align: center;">'
                '<img src="{}" style="max-width: 300px; max-height: 200px; border-radius: 4px; border: 2px solid #ddd;">'
                '<br/><small style="color: #666;">Watermarked Version</small>'
                "</div>",
                obj.image.url,
            )
        return "No image"

    image_preview.short_description = "Watermarked Image"

    def approve_image_suggestions(self, request, queryset):
        approved_count = 0
        failed_count = 0

        for suggestion in queryset.filter(status="pending"):
            if self._apply_image_suggestion(suggestion, request.user):
                suggestion.status = "approved"
                suggestion.reviewed_by = request.user
                suggestion.reviewed_at = timezone.now()
                suggestion.save()
                approved_count += 1
            else:
                failed_count += 1
                # Keep the suggestion as pending but add admin notes
                suggestion.admin_notes = (
                    "Failed to apply automatically. Please review manually."
                )
                suggestion.save()

        if approved_count:
            messages.success(
                request,
                f"Successfully approved and applied {approved_count} image suggestion(s)",
            )
        if failed_count:
            messages.error(
                request,
                f"{failed_count} image suggestion(s) failed to apply. Check logs and admin notes for details.",
            )

    approve_image_suggestions.short_description = "Approve selected image suggestions"

    def reject_image_suggestions(self, request, queryset):
        rejected_count = 0
        for suggestion in queryset.filter(status="pending"):
            suggestion.status = "rejected"
            suggestion.reviewed_by = request.user
            suggestion.reviewed_at = timezone.now()
            suggestion.save()
            rejected_count += 1

        if rejected_count:
            messages.success(
                request, f"Successfully rejected {rejected_count} image suggestion(s)"
            )

    reject_image_suggestions.short_description = "Reject selected image suggestions"

    def reapply_watermark_action(self, request, queryset):
        """Reapply watermark to selected images with current settings"""
        success_count = 0
        failed_count = 0

        for suggestion in queryset:
            if not suggestion.original_image:
                failed_count += 1
                messages.warning(
                    request, f"Skipped {suggestion}: No original image available"
                )
                continue

            try:
                if suggestion.reapply_watermark():
                    success_count += 1
                else:
                    failed_count += 1
            except Exception as e:
                failed_count += 1
                logger.error(f"Error reapplying watermark to {suggestion.pk}: {str(e)}")

        if success_count:
            messages.success(
                request, f"Successfully reapplied watermark to {success_count} image(s)"
            )
        if failed_count:
            messages.error(
                request, f"Failed to reapply watermark to {failed_count} image(s)"
            )

    reapply_watermark_action.short_description = (
        "🔄 Reapply watermark with current settings"
    )

    def apply_watermark_to_existing(self, request, queryset):
        """Apply watermark to images that don't have one yet"""
        from utils.watermark import apply_watermark_to_uploaded_file
        from django.core.files.base import ContentFile
        import io
        from pathlib import Path

        success_count = 0
        failed_count = 0
        skipped_count = 0

        for suggestion in queryset:
            # Skip if already has watermark (has original_image)
            if suggestion.original_image:
                skipped_count += 1
                continue

            if not suggestion.image:
                failed_count += 1
                continue

            try:
                # Save current image as original
                suggestion.original_image = suggestion.image
                suggestion.save(update_fields=["original_image"])

                # Apply watermark to the image
                image_file = suggestion.original_image.file
                image_file.seek(0)

                watermarked_img = apply_watermark_to_uploaded_file(image_file)

                # Convert PIL image to file
                img_io = io.BytesIO()
                original_ext = Path(suggestion.original_image.name).suffix.lower()

                if original_ext in [".jpg", ".jpeg"]:
                    watermarked_img = watermarked_img.convert("RGB")
                    watermarked_img.save(img_io, format="JPEG", quality=95)
                else:
                    watermarked_img.save(img_io, format="PNG")

                img_io.seek(0)

                # Save watermarked version
                watermarked_filename = f"wm_{Path(suggestion.original_image.name).name}"
                suggestion.image.save(
                    watermarked_filename, ContentFile(img_io.read()), save=True
                )

                success_count += 1

            except Exception as e:
                failed_count += 1
                logger.error(
                    f"Error applying watermark to {suggestion.pk}: {str(e)}",
                    exc_info=True,
                )

        if success_count:
            messages.success(
                request, f"Successfully applied watermark to {success_count} image(s)"
            )
        if skipped_count:
            messages.info(
                request,
                f"Skipped {skipped_count} image(s) that already have watermarks",
            )
        if failed_count:
            messages.error(
                request, f"Failed to apply watermark to {failed_count} image(s)"
            )

    apply_watermark_to_existing.short_description = (
        "💧 Apply watermark to selected images"
    )

    def _apply_image_suggestion(self, suggestion, reviewer):
        """Apply an approved image suggestion to the place"""
        try:
            import logging

            logger = logging.getLogger(__name__)

            # Reload the place: bulk actions hold one place instance per
            # suggestion, so an earlier approval would otherwise be overwritten.
            place = suggestion.place
            place.refresh_from_db()
            image_url = suggestion.image.url

            logger.info(
                f"Applying image suggestion for place '{place.name}' (ID: {place.pk}): {image_url}"
            )

            # Add to the place's photo_urls array
            if place.photo_urls is None:
                place.photo_urls = []
                logger.info(f"Initialized empty photo_urls array for place {place.pk}")

            # Check if image URL already exists to avoid duplicates
            if image_url in place.photo_urls:
                logger.warning(f"Image URL already exists in place photos: {image_url}")
                return True  # Consider this a success since the image is already there

            # Add the new image URL to the array
            place.photo_urls.append(image_url)
            place.save()

            logger.info(
                f"Successfully added image to place {place.pk}. Total photos: {len(place.photo_urls)}"
            )
            return True

        except Exception as e:
            import logging

            logger = logging.getLogger(__name__)
            logger.error(
                f"Error applying image suggestion {suggestion.pk}: {str(e)}",
                exc_info=True,
            )
            return False


@admin.register(SearchQuery)
class SearchQueryAdmin(admin.ModelAdmin):
    """Admin for search analytics"""

    list_display = ["query", "results_count", "category_filter", "user", "created_at"]
    list_filter = ["category_filter", "created_at"]
    search_fields = ["query", "user__username", "user__email"]
    readonly_fields = [
        "query",
        "user",
        "session_key",
        "results_count",
        "category_filter",
        "clicked_place",
        "created_at",
    ]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        # Add analytics summary to changelist
        from django.db.models import Count
        from django.utils import timezone
        from datetime import timedelta

        last_7_days = timezone.now() - timedelta(days=7)
        last_30_days = timezone.now() - timedelta(days=30)

        # Popular searches
        popular = (
            SearchQuery.objects.filter(created_at__gte=last_7_days, results_count__gt=0)
            .values("query")
            .annotate(count=Count("id"))
            .order_by("-count")[:10]
        )

        # Zero result searches
        zero_results = (
            SearchQuery.objects.filter(created_at__gte=last_30_days, results_count=0)
            .values("query")
            .annotate(count=Count("id"))
            .order_by("-count")[:10]
        )

        extra_context = extra_context or {}
        extra_context["popular_searches"] = list(popular)
        extra_context["zero_result_searches"] = list(zero_results)

        return super().changelist_view(request, extra_context=extra_context)


@admin.register(ProximityLocation)
class ProximityLocationAdmin(admin.ModelAdmin):
    """Admin for managing proximity search locations"""

    list_display = [
        "name",
        "name_korean",
        "latitude",
        "longitude",
        "aliases_display",
        "is_active",
        "updated_at",
    ]
    list_filter = ["is_active", "created_at"]
    search_fields = ["name", "name_korean", "aliases"]
    list_editable = ["is_active"]
    ordering = ["name"]

    fieldsets = (
        (
            "Location Info",
            {
                "fields": ("name", "name_korean", "aliases"),
                "description": 'Enter the location name in English and Korean. Aliases should be comma-separated alternative names (e.g., "sincheon, 신천동").',
            },
        ),
        (
            "Coordinates",
            {
                "fields": ("latitude", "longitude"),
                "description": "Center coordinates for this location. Used for proximity searches.",
            },
        ),
        (
            "Status",
            {
                "fields": ("is_active",),
            },
        ),
    )

    def aliases_display(self, obj):
        """Display aliases in a truncated format"""
        if obj.aliases:
            aliases = obj.aliases[:50] + "..." if len(obj.aliases) > 50 else obj.aliases
            return aliases
        return "-"

    aliases_display.short_description = "Aliases"

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # Clear the cached locations so new data is used
        from django.core.cache import cache

        cache.delete("proximity_locations")
        messages.success(request, "Proximity location saved. Cache cleared.")


@admin.register(BusinessHoursSuggestion)
class BusinessHoursSuggestionAdmin(admin.ModelAdmin):
    """Admin for managing business hours suggestions with approval workflow"""

    list_display = (
        "id",
        "place_name",
        "suggested_by",
        "status_badge",
        "hours_preview",
        "created_at",
        "quick_actions",
    )
    list_display_links = ("id", "place_name")
    list_filter = ("status", "created_at", "is_24_hours")
    search_fields = ("place__name", "suggested_by__username", "reason")
    readonly_fields = (
        "place",
        "suggested_by",
        "suggested_hours_display",
        "current_hours_display",
        "created_at",
        "reviewed_by",
        "reviewed_at",
    )
    list_per_page = 25
    ordering = ("-created_at",)

    actions = ["bulk_approve", "bulk_reject"]

    fieldsets = (
        ("Suggestion Info", {"fields": ("place", "suggested_by", "status", "reason")}),
        (
            "Hours Comparison",
            {
                "fields": ("current_hours_display", "suggested_hours_display"),
                "description": "Compare current hours with suggested changes",
            },
        ),
        (
            "Suggested Data (Editable)",
            {
                "fields": ("is_24_hours", "suggested_hours", "suggested_notes"),
                "description": "You can edit these fields before approving",
            },
        ),
        (
            "Review Info",
            {
                "fields": ("reviewed_by", "reviewed_at", "admin_notes"),
                "classes": ("collapse",),
            },
        ),
    )

    def place_name(self, obj):
        return obj.place.name

    place_name.short_description = "Place"
    place_name.admin_order_field = "place__name"

    def status_badge(self, obj):
        colors = {"pending": "#fbbf24", "approved": "#10b981", "rejected": "#ef4444"}
        color = colors.get(obj.status, "#6b7280")
        return format_html(
            '<span style="background-color: {}; color: white; padding: 4px 8px; border-radius: 12px; font-size: 11px; font-weight: bold;">{}</span>',
            color,
            obj.get_status_display().upper(),
        )

    status_badge.short_description = "Status"

    def hours_preview(self, obj):
        if obj.is_24_hours:
            return format_html('<span style="color: #059669;">Open 24 Hours</span>')

        # Show a brief summary of suggested hours
        try:
            hours = obj.suggested_hours
            if isinstance(hours, str):
                hours = json.loads(hours)

            open_days = sum(1 for v in hours.values() if v != "closed")
            return format_html(
                '<span style="color: #2563eb;">{} days set</span>', open_days
            )
        except:
            return "-"

    hours_preview.short_description = "Hours Summary"

    def quick_actions(self, obj):
        """Display quick action buttons for pending suggestions"""
        if obj.status == "pending":
            return format_html(
                '<div style="white-space: nowrap;">'
                '<a href="{}?status=approved" '
                'style="background: #059669; color: white; padding: 4px 8px; '
                'border-radius: 4px; text-decoration: none; margin-right: 4px; font-size: 11px;">'
                "✅ Approve</a>"
                '<a href="{}?status=rejected" '
                'style="background: #dc2626; color: white; padding: 4px 8px; '
                'border-radius: 4px; text-decoration: none; font-size: 11px;">'
                "❌ Reject</a>"
                "</div>",
                f"/admin/places/businesshourssuggestion/{obj.pk}/change/",
                f"/admin/places/businesshourssuggestion/{obj.pk}/change/",
            )
        return "-"

    quick_actions.short_description = "Actions"

    def get_changeform_initial_data(self, request):
        """Pre-fill status field from query parameter for quick actions"""
        initial = super().get_changeform_initial_data(request)
        status = request.GET.get("status")
        if status in ["approved", "rejected"]:
            initial["status"] = status
        return initial

    def current_hours_display(self, obj):
        """Display current business hours for comparison"""
        try:
            bh = obj.place.business_hours
            if bh.is_24_hours:
                return format_html(
                    '<p style="color: #059669; font-weight: bold;">Open 24 Hours</p>'
                )

            html = ['<table style="border-collapse: collapse; width: 100%;">']
            day_names = [
                "Monday",
                "Tuesday",
                "Wednesday",
                "Thursday",
                "Friday",
                "Saturday",
                "Sunday",
            ]

            for day_num, day_name in enumerate(day_names):
                slots = bh.time_slots.filter(day_of_week=day_num)
                if slots.exists():
                    closed = any(s.is_closed for s in slots)
                    if closed:
                        hours_str = '<span style="color: #dc2626;">Closed</span>'
                    else:
                        hours_parts = [
                            f"{s.open_time.strftime('%H:%M')} - {s.close_time.strftime('%H:%M')}"
                            for s in slots
                            if s.open_time and s.close_time
                        ]
                        hours_str = ", ".join(hours_parts) if hours_parts else "Not set"
                else:
                    hours_str = '<span style="color: #9ca3af;">Not set</span>'

                html.append(
                    f'<tr style="border-bottom: 1px solid #e5e7eb;"><td style="padding: 4px 8px; font-weight: 500;">{day_name}</td><td style="padding: 4px 8px;">{hours_str}</td></tr>'
                )

            html.append("</table>")
            if bh.notes:
                html.append(
                    f'<p style="margin-top: 8px; color: #6b7280; font-style: italic;">Note: {bh.notes}</p>'
                )

            return format_html("".join(html))
        except Exception:
            return format_html(
                '<p style="color: #9ca3af; font-style: italic;">No hours set for this place</p>'
            )

    current_hours_display.short_description = "Current Hours"

    def suggested_hours_display(self, obj):
        """Display suggested hours in a readable format"""
        if obj.is_24_hours:
            return format_html(
                '<p style="color: #059669; font-weight: bold;">Open 24 Hours</p>'
            )

        try:
            hours = obj.suggested_hours
            if isinstance(hours, str):
                hours = json.loads(hours)

            html = ['<table style="border-collapse: collapse; width: 100%;">']
            day_names = [
                "Monday",
                "Tuesday",
                "Wednesday",
                "Thursday",
                "Friday",
                "Saturday",
                "Sunday",
            ]

            for day_num, day_name in enumerate(day_names):
                day_data = hours.get(str(day_num))
                if day_data == "closed":
                    hours_str = '<span style="color: #dc2626;">Closed</span>'
                elif isinstance(day_data, list):
                    hours_parts = [
                        f"{slot.get('open', '?')} - {slot.get('close', '?')}"
                        for slot in day_data
                    ]
                    hours_str = ", ".join(hours_parts)
                else:
                    hours_str = '<span style="color: #9ca3af;">Not set</span>'

                html.append(
                    f'<tr style="border-bottom: 1px solid #e5e7eb;"><td style="padding: 4px 8px; font-weight: 500;">{day_name}</td><td style="padding: 4px 8px;">{hours_str}</td></tr>'
                )

            html.append("</table>")
            if obj.suggested_notes:
                html.append(
                    f'<p style="margin-top: 8px; color: #6b7280; font-style: italic;">Note: {obj.suggested_notes}</p>'
                )

            return format_html("".join(html))
        except Exception as e:
            return format_html(
                f'<p style="color: #dc2626;">Error parsing hours: {str(e)}</p>'
            )

    suggested_hours_display.short_description = "Suggested Hours"

    def save_model(self, request, obj, form, change):
        """Handle approval workflow when status changes to approved"""
        previous_status = None
        if change and obj.pk:
            previous_status = (
                BusinessHoursSuggestion.objects.filter(pk=obj.pk)
                .values_list("status", flat=True)
                .first()
            )

        status_changed_to_approved = (
            obj.status == "approved" and previous_status != "approved"
        )

        if status_changed_to_approved:
            obj.reviewed_by = request.user
            obj.reviewed_at = timezone.now()

            if self._apply_hours_suggestion(obj):
                messages.success(
                    request,
                    f'✅ Hours suggestion approved and applied to "{obj.place.name}"',
                )
            else:
                obj.status = "pending"
                obj.admin_notes = f"Failed to apply on {timezone.now().strftime('%Y-%m-%d %H:%M')}. Please review manually."
                messages.error(
                    request, f"❌ Failed to apply hours suggestion. Check admin notes."
                )

        elif obj.status == "rejected" and previous_status != "rejected":
            obj.reviewed_by = request.user
            obj.reviewed_at = timezone.now()
            messages.info(request, f'Hours suggestion rejected for "{obj.place.name}"')

        super().save_model(request, obj, form, change)

    def _apply_hours_suggestion(self, suggestion):
        """Apply approved suggestion to the place's business hours"""
        try:
            place = suggestion.place
            hours = suggestion.suggested_hours
            if isinstance(hours, str):
                hours = json.loads(hours)

            with transaction.atomic():
                # Get or create BusinessHours for this place
                bh, created = BusinessHours.objects.get_or_create(place=place)
                bh.is_24_hours = suggestion.is_24_hours
                bh.notes = suggestion.suggested_notes
                bh.save()

                # Clear existing time slots to avoid stale data
                bh.time_slots.all().delete()

                if not suggestion.is_24_hours:
                    # Create new time slots from suggestion
                    for day_str, day_data in hours.items():
                        day_num = int(day_str)
                        if day_data == "closed":
                            TimeSlot.objects.create(
                                business_hours=bh, day_of_week=day_num, is_closed=True
                            )
                        elif isinstance(day_data, list):
                            for slot in day_data:
                                from datetime import datetime

                                open_time = (
                                    datetime.strptime(slot["open"], "%H:%M").time()
                                    if slot.get("open")
                                    else None
                                )
                                close_time = (
                                    datetime.strptime(slot["close"], "%H:%M").time()
                                    if slot.get("close")
                                    else None
                                )
                                TimeSlot.objects.create(
                                    business_hours=bh,
                                    day_of_week=day_num,
                                    open_time=open_time,
                                    close_time=close_time,
                                )

            logger.info(
                f"Applied hours suggestion {suggestion.pk} to place {place.name}"
            )
            return True

        except Exception as e:
            logger.error(
                f"Error applying hours suggestion {suggestion.pk}: {str(e)}",
                exc_info=True,
            )
            return False

    @admin.action(description="Approve selected suggestions")
    def bulk_approve(self, request, queryset):
        count = 0
        for suggestion in queryset.filter(status="pending"):
            suggestion.status = "approved"
            suggestion.reviewed_by = request.user
            suggestion.reviewed_at = timezone.now()
            if self._apply_hours_suggestion(suggestion):
                suggestion.save()
                count += 1
            else:
                messages.warning(
                    request, f'Failed to apply hours for "{suggestion.place.name}"'
                )
        messages.success(request, f"✅ Approved and applied {count} hours suggestions")

    @admin.action(description="Reject selected suggestions")
    def bulk_reject(self, request, queryset):
        count = queryset.filter(status="pending").update(
            status="rejected", reviewed_by=request.user, reviewed_at=timezone.now()
        )
        messages.success(request, f"❌ Rejected {count} hours suggestions")
