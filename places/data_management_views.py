"""
Data management views for import/export functionality.
Provides standalone admin interface for place data management.
"""

import json
import csv
import io
from datetime import datetime

from django.shortcuts import render, redirect
from django.http import JsonResponse, HttpResponse
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.views.decorators.http import require_http_methods, require_POST
from django.core.paginator import Paginator

from .models import HalalPlace
from .export_utils import PlaceExporter
from .import_utils import PlaceImporter


@staff_member_required
def data_management_dashboard(request):
    """
    Main dashboard for data import/export operations.
    Shows statistics and provides access to import/export functions.
    """
    # Get counts for different statuses
    total_places = HalalPlace.objects.count()
    approved_count = HalalPlace.objects.filter(status="approved").count()
    pending_count = HalalPlace.objects.filter(status="pending").count()
    rejected_count = HalalPlace.objects.filter(status="rejected").count()
    archived_count = HalalPlace.objects.filter(status="archived").count()

    # Get recent import history (if we had a model for it)
    # For now, just show recent places
    recent_places = HalalPlace.objects.order_by("-created_at")[:10]

    # Get places by category
    categories = {}
    for code, name in HalalPlace.CATEGORY_CHOICES:
        categories[name] = HalalPlace.objects.filter(category=code).count()

    context = {
        "total_places": total_places,
        "approved_places": approved_count,
        "pending_places": pending_count,
        "rejected_count": rejected_count,
        "archived_count": archived_count,
        "recent_places": recent_places,
        "categories": categories,
        "title": "Data Management Dashboard",
    }

    return render(request, "admin/places/data_management/dashboard.html", context)


@staff_member_required
def export_places_view(request):
    """
    Export form and handler for downloading place data.
    Supports JSON, CSV, Excel, and GeoJSON formats.
    """
    if request.method == "POST":
        # Get export parameters
        format_type = request.POST.get("format", "json")
        status_filter = request.POST.get("status", "all")
        include_hours = request.POST.get("include_business_hours", "on") == "on"

        # Build queryset based on filters
        queryset = HalalPlace.objects.all()
        if status_filter != "all":
            queryset = queryset.filter(status=status_filter)

        # Create exporter
        exporter = PlaceExporter(queryset, include_business_hours=include_hours)

        # Generate filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        try:
            # Get appropriate HTTP response
            response = exporter.get_http_response(format_type)
            messages.success(
                request,
                f"Successfully exported {queryset.count()} places as {format_type.upper()}",
            )
            return response
        except ValueError as e:
            messages.error(request, f"Export error: {str(e)}")
            return redirect("places_data_management:data_management_dashboard")

    # GET request - show export form
    context = {
        "status_choices": HalalPlace.STATUS_CHOICES,
        "format_choices": [
            ("json", "JSON"),
            ("csv", "CSV"),
            ("excel", "Excel (.xlsx)"),
            ("geojson", "GeoJSON"),
        ],
        "title": "Export Places",
    }

    return render(request, "admin/places/data_management/export.html", context)


@staff_member_required
def import_places_view(request):
    """
    Import form with file upload for place data.
    Supports JSON and CSV formats with preview and validation.
    """
    if request.method == "POST":
        # Check if this is a confirm import from preview
        if request.POST.get("confirm_import") == "true":
            # Get stored file content from session
            content = request.session.get("import_preview_content")
            file_format = request.session.get("import_preview_format")
            update_existing = request.session.get(
                "import_preview_update_existing", True
            )

            if not content:
                messages.error(
                    request, "Preview data expired. Please upload file again."
                )
                return redirect("places_data_management:import_places")

            # Perform actual import (dry_run=False)
            importer = PlaceImporter(
                user=request.user, dry_run=False, update_existing=update_existing
            )

            try:
                if file_format == "json":
                    result = importer.import_json(content)
                elif file_format == "csv":
                    result = importer.import_csv(content)
                else:
                    messages.error(request, f"Unsupported format: {file_format}")
                    return redirect("places_data_management:import_places")

                # Build result message, surfacing errors like the direct import
                message_parts = []
                if result.created_count > 0:
                    message_parts.append(f"{result.created_count} created")
                if result.updated_count > 0:
                    message_parts.append(f"{result.updated_count} updated")
                if result.skipped_count > 0:
                    message_parts.append(f"{result.skipped_count} skipped")
                if result.error_count > 0:
                    message_parts.append(f"{result.error_count} errors")

                if (
                    result.error_count > 0
                    and result.created_count == 0
                    and result.updated_count == 0
                ):
                    error_word = "error" if result.error_count == 1 else "errors"

                    messages.error(

                        request,

                        f"Import failed: {result.error_count} {error_word}. "

                        "No places were imported.",

                    )
                elif message_parts:
                    status_msg = f"Import complete: {', '.join(message_parts)}."
                    if result.error_count > 0:
                        messages.warning(request, status_msg)
                    else:
                        messages.success(request, status_msg)

                # Clear session
                for key in [
                    "import_preview_content",
                    "import_preview_format",
                    "import_preview_filename",
                    "import_preview_update_existing",
                ]:
                    request.session.pop(key, None)

                return redirect("places_data_management:data_management_dashboard")

            except Exception as e:
                # Clear session on error
                for key in [
                    "import_preview_content",
                    "import_preview_format",
                    "import_preview_filename",
                    "import_preview_update_existing",
                ]:
                    request.session.pop(key, None)
                messages.error(request, f"Import failed: {str(e)}")
                return redirect("places_data_management:import_places")

        # Check if file was uploaded
        if "import_file" not in request.FILES:
            messages.error(request, "Please select a file to import.")
            return redirect("places_data_management:import_places")

        uploaded_file = request.FILES["import_file"]
        file_format = request.POST.get("format", "auto")
        dry_run = request.POST.get("dry_run", "off") == "on"
        update_existing = request.POST.get("update_existing", "on") == "on"

        # Determine format from filename if auto
        if file_format == "auto":
            filename = uploaded_file.name.lower()
            if filename.endswith(".json") or filename.endswith(".geojson"):
                file_format = "json"
            elif filename.endswith(".csv"):
                file_format = "csv"
            else:
                messages.error(
                    request,
                    "Could not determine file format. Please specify the format.",
                )
                return redirect("places_data_management:import_places")

        # Read file content
        try:
            content = uploaded_file.read().decode("utf-8")
        except UnicodeDecodeError:
            messages.error(request, "File must be UTF-8 encoded.")
            return redirect("places_data_management:import_places")
        except Exception as e:
            messages.error(request, f"Error reading file: {str(e)}")
            return redirect("places_data_management:import_places")

        # Validate file size (10MB limit)
        MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
        if len(content) > MAX_FILE_SIZE:
            messages.error(
                request,
                f"File too large. Maximum size is {MAX_FILE_SIZE // (1024 * 1024)}MB.",
            )
            return redirect("places_data_management:import_places")

        # Create importer
        importer = PlaceImporter(
            user=request.user, dry_run=dry_run, update_existing=update_existing
        )

        # Perform import
        try:
            if file_format == "json":
                result = importer.import_json(content)
            elif file_format == "csv":
                result = importer.import_csv(content)
            else:
                messages.error(request, f"Unsupported format: {file_format}")
                return redirect("places_data_management:import_places")

            # Build success message
            if dry_run:
                action = "would be"
            else:
                action = "were"

            message_parts = []
            if result.created_count > 0:
                message_parts.append(f"{result.created_count} created")
            if result.updated_count > 0:
                message_parts.append(f"{result.updated_count} updated")
            if result.skipped_count > 0:
                message_parts.append(f"{result.skipped_count} skipped")
            if result.error_count > 0:
                message_parts.append(f"{result.error_count} errors")

            if dry_run:
                # Show detailed preview on same page
                context = {
                    "format_choices": [
                        ("auto", "Auto-detect"),
                        ("json", "JSON / GeoJSON"),
                        ("csv", "CSV"),
                    ],
                    "title": "Import Places - Preview",
                    "import_result": result.get_summary(),
                    "row_details": result.row_details[:20],  # First 20 rows only
                    "file_format": file_format,
                    "update_existing": "on" if update_existing else "off",
                    "show_results": True,
                }
                # Store content in session for confirm
                request.session["import_preview_content"] = content
                request.session["import_preview_format"] = file_format
                request.session["import_preview_update_existing"] = update_existing

                return render(
                    request, "admin/places/data_management/import.html", context
                )

            # Build result message for actual import
            if (
                result.error_count > 0
                and result.created_count == 0
                and result.updated_count == 0
            ):
                error_word = "error" if result.error_count == 1 else "errors"

                messages.error(

                    request,

                    f"Import failed: {result.error_count} {error_word}. "

                    "No places were imported.",

                )
            elif message_parts:
                status_msg = f"Import complete: {', '.join(message_parts)}."
                if result.error_count > 0:
                    messages.warning(request, status_msg)
                else:
                    messages.success(request, status_msg)

            # Store import result in session for detailed view
            request.session["last_import_result"] = result.to_dict()

            return redirect("places_data_management:data_management_dashboard")

        except Exception as e:
            messages.error(request, f"Import failed: {str(e)}")
            return redirect("places_data_management:import_places")

    # GET request - show import form
    context = {
        "format_choices": [
            ("auto", "Auto-detect"),
            ("json", "JSON / GeoJSON"),
            ("csv", "CSV"),
        ],
        "title": "Import Places",
    }

    return render(request, "admin/places/data_management/import.html", context)


@staff_member_required
@require_POST
def validate_import_file_ajax(request):
    """
    AJAX endpoint for validating import file before actual import.
    Returns JSON with validation results and preview data.
    """
    try:
        # Get file from request
        if "file" not in request.FILES:
            return JsonResponse({"valid": False, "errors": ["No file provided"]})

        uploaded_file = request.FILES["file"]
        file_format = request.POST.get("format", "auto")

        # Determine format
        if file_format == "auto":
            filename = uploaded_file.name.lower()
            if filename.endswith(".json") or filename.endswith(".geojson"):
                file_format = "json"
            elif filename.endswith(".csv"):
                file_format = "csv"
            else:
                return JsonResponse(
                    {
                        "valid": False,
                        "errors": ["Could not determine file format from extension"],
                    }
                )

        # Read and validate
        try:
            content = uploaded_file.read().decode("utf-8")
        except UnicodeDecodeError:
            return JsonResponse(
                {"valid": False, "errors": ["File must be UTF-8 encoded"]}
            )

        # Create importer for validation
        importer = PlaceImporter(dry_run=True)
        is_valid, errors = importer.validate_file(content, file_format)

        # Try to get preview data
        preview_data = []
        try:
            if file_format == "json":
                data = json.loads(content)
                if isinstance(data, dict) and data.get("type") == "FeatureCollection":
                    preview_data = data.get("features", [])[:5]
                elif isinstance(data, list):
                    preview_data = data[:5]
                elif isinstance(data, dict) and "places" in data:
                    preview_data = data["places"][:5]
            elif file_format == "csv":
                reader = csv.DictReader(io.StringIO(content))
                preview_data = list(reader)[:5]
        except Exception:
            pass  # Preview not critical for validation

        response_data = {
            "valid": is_valid,
            "errors": errors,
            "preview": preview_data,
            "format": file_format,
        }

        return JsonResponse(response_data)

    except Exception as e:
        return JsonResponse({"valid": False, "errors": [f"Validation error: {str(e)}"]})


@staff_member_required
def import_template_download(request, format_type):
    """
    Download import template files for various formats.
    Provides empty templates with correct column headers.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if format_type == "csv":
        # Generate CSV template
        output = io.StringIO()
        writer = csv.writer(output)

        headers = [
            "name",
            "description",
            "category",
            "address",
            "phone_number",
            "website",
            "google_map_link",
            "kakao_map_link",
            "naver_map_link",
            "latitude",
            "longitude",
            "photo_urls",
            "status",
            "temporary_closure_until",
            "temporary_closure_reason",
        ]
        writer.writerow(headers)

        # Add example row
        example = [
            "Example Halal Restaurant",
            "A great halal restaurant in Seoul",
            "restaurant",
            "123 Halal Street, Seoul",
            "+82-2-1234-5678",
            "https://example.com",
            "https://maps.google.com/?q=37.5665,126.9780",
            "",
            "",
            "37.5665",
            "126.9780",
            "https://example.com/photo1.jpg;https://example.com/photo2.jpg",
            "pending",
            "",
            "",
        ]
        writer.writerow(example)

        content = output.getvalue()
        response = HttpResponse(content, content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="halal_places_template_{timestamp}.csv"'
        )

    elif format_type == "json":
        # Generate JSON template
        template = {
            "places": [
                {
                    "name": "Example Halal Restaurant",
                    "description": "A great halal restaurant in Seoul",
                    "category": "restaurant",
                    "address": "123 Halal Street, Seoul",
                    "phone_number": "+82-2-1234-5678",
                    "website": "https://example.com",
                    "google_map_link": "https://maps.google.com/?q=37.5665,126.9780",
                    "kakao_map_link": "",
                    "naver_map_link": "",
                    "latitude": 37.5665,
                    "longitude": 126.9780,
                    "photo_urls": [
                        "https://example.com/photo1.jpg",
                        "https://example.com/photo2.jpg",
                    ],
                    "status": "pending",
                    "temporary_closure_until": None,
                    "temporary_closure_reason": "",
                }
            ]
        }

        content = json.dumps(template, indent=2)
        response = HttpResponse(content, content_type="application/json")
        response["Content-Disposition"] = (
            f'attachment; filename="halal_places_template_{timestamp}.json"'
        )

    else:
        messages.error(request, f"Unknown template format: {format_type}")
        return redirect("places_data_management:import_places")

    return response
