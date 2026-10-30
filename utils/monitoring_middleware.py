"""
Monitoring middleware for tracking requests, performance, and admin actions.
"""
import time
import random
from django.conf import settings
from django.utils import timezone
from django.db import connection, transaction
from django.core.cache import cache
from utils.models import RequestLog, AdminAction, hash_ip, hash_user_agent
import logging

logger = logging.getLogger(__name__)

NON_PAGE_PATH_SUFFIXES = ('.php', '.env', '.git', '.yaml', '.yml', '.asp', '.aspx', '.jsp', '.cgi')
BOT_PROBE_PATHS = ('/robots.txt', '/sitemap.xml', '/favicon.ico', '/.well-known/traffic-advice')


class MonitoringMiddleware:
    """
    Middleware to track request performance, errors, and database queries.
    Implements sampling to avoid overwhelming the database with logs.
    """
    
    def __init__(self, get_response):
        self.get_response = get_response
        self.enabled = getattr(settings, 'MONITORING_ENABLED', True)
        self.sample_rate = getattr(settings, 'MONITORING_SAMPLE_RATE', 0.01)  # 1%
        self.slow_threshold_ms = getattr(settings, 'MONITORING_SLOW_THRESHOLD_MS', 1000)
    
    def __call__(self, request):
        if not self.enabled:
            return self.get_response(request)
        
        # Skip logging for static files and media files (performance optimization)
        path = request.path
        if (path.startswith('/static/') or 
            path.startswith('/media/') or 
            path.startswith('/favicon.ico') or
            path.startswith('/robots.txt')):
            return self.get_response(request)
        
        # Skip monitoring for obvious bot/scanner probes
        if path.endswith(NON_PAGE_PATH_SUFFIXES) or path in BOT_PROBE_PATHS:
            return self.get_response(request)
        
        # Start timing
        start_time = time.time()
        
        # Track initial query count
        initial_queries = len(connection.queries) if settings.DEBUG else 0
        
        # Track cache stats (simplified - actual implementation would need cache backend patching)
        cache_hits_before = getattr(cache, '_hits', 0)
        cache_misses_before = getattr(cache, '_misses', 0)
        
        # Process request
        response = None
        error_type = None
        error_message = None
        
        try:
            response = self.get_response(request)
            return response
        except Exception as e:
            error_type = type(e).__name__
            error_message = str(e)
            raise
        finally:
            # Calculate metrics
            response_time_ms = (time.time() - start_time) * 1000
            
            # Get query count
            query_count = len(connection.queries) - initial_queries if settings.DEBUG else 0
            
            # Get cache stats
            cache_hits = getattr(cache, '_hits', 0) - cache_hits_before
            cache_misses = getattr(cache, '_misses', 0) - cache_misses_before
            
            # Determine if we should log this request
            should_log = self._should_log_request(
                request,
                response,
                response_time_ms,
                error_type
            )
            
            if should_log:
                # Use async logging to avoid blocking response
                self._log_request_async(
                    request,
                    response,
                    response_time_ms,
                    query_count,
                    cache_hits,
                    cache_misses,
                    error_type,
                    error_message
                )
    
    def _should_log_request(self, request, response, response_time_ms, error_type):
        """
        Determine if request should be logged based on sampling rules.
        Always log errors and slow requests, sample normal requests.
        Higher sampling for high-traffic endpoints.
        """
        # Always log errors
        if error_type or (response and response.status_code >= 400):
            return True
        
        # Always log slow requests
        if response_time_ms > self.slow_threshold_ms:
            return True
        
        # Higher sampling rate for high-traffic API endpoints
        path = request.path
        high_traffic_paths = ['/places/', '/api/', '/explore']
        is_high_traffic = any(path.startswith(prefix) for prefix in high_traffic_paths)
        
        # Use higher sample rate for high-traffic endpoints (5% vs 1%)
        sample_rate = self.sample_rate * 5 if is_high_traffic else self.sample_rate
        
        # Sample normal requests
        return random.random() < sample_rate
    
    def _log_request_async(self, request, response, response_time_ms, query_count,
                          cache_hits, cache_misses, error_type, error_message):
        """
        Log request to database asynchronously to avoid blocking response.
        Uses threading to defer database write.
        """
        import threading
        
        def log_in_background():
            try:
                # Get user info
                user = request.user if request.user.is_authenticated else None
                is_staff = user.is_staff if user else False
                
                # Get IP and user agent (hashed for privacy)
                ip_address = self._get_client_ip(request)
                ip_hash = hash_ip(ip_address)
                user_agent = request.META.get('HTTP_USER_AGENT', '')
                user_agent_hash = hash_user_agent(user_agent)
                
                # Create log entry
                RequestLog.objects.create(
                    path=request.path[:500],  # Truncate long paths
                    method=request.method[:10],
                    status_code=response.status_code if response else 500,
                    response_time_ms=response_time_ms,
                    user=user,
                    is_staff=is_staff,
                    ip_hash=ip_hash,
                    user_agent_hash=user_agent_hash,
                    db_query_count=query_count,
                    cache_hits=cache_hits,
                    cache_misses=cache_misses,
                    error_type=(error_type or '')[:100],
                    error_message=error_message or ''
                )
            except Exception as e:
                # Don't let monitoring errors break the application
                logger.error(f"Failed to log request: {e}", exc_info=True)
        
        # Execute in background thread (daemon so it doesn't block shutdown)
        thread = threading.Thread(target=log_in_background, daemon=True)
        thread.start()
    
    def _get_client_ip(self, request):
        """Extract client IP address from request."""
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0].strip()
        else:
            ip = request.META.get('REMOTE_ADDR', '')
        return ip


class AdminActionMiddleware:
    """
    Middleware to track admin actions for audit trail.
    This works by hooking into Django admin's ModelAdmin save/delete methods.
    """
    
    def __init__(self, get_response):
        self.get_response = get_response
        self.enabled = getattr(settings, 'MONITORING_ENABLED', True)
        # Patch Django admin methods on initialization
        if self.enabled:
            self._patch_admin_methods()
    
    def __call__(self, request):
        # Store request in thread-local for access in patched methods
        if self.enabled:
            _thread_local.request = request
        
        response = self.get_response(request)
        
        # Clean up thread-local
        if self.enabled and hasattr(_thread_local, 'request'):
            delattr(_thread_local, 'request')
        
        return response
    
    def _patch_admin_methods(self):
        """
        Patch Django admin methods to log actions.
        This is done once during middleware initialization.
        """
        from django.contrib.admin.options import ModelAdmin
        from django.contrib.contenttypes.models import ContentType
        
        # Store original methods
        original_save_model = ModelAdmin.save_model
        original_delete_model = ModelAdmin.delete_model
        
        def patched_save_model(self, request, obj, form, change):
            """Patched save_model to log admin actions."""
            # Determine action type
            action_type = 'update' if change else 'create'
            
            # Track changes for updates
            changes = {}
            if change and hasattr(form, 'changed_data'):
                for field in form.changed_data:
                    if field in form.cleaned_data:
                        changes[field] = {
                            'old': str(getattr(obj, field, None)),
                            'new': str(form.cleaned_data[field])
                        }
            
            # Call original method
            result = original_save_model(self, request, obj, form, change)
            
            # Log action
            try:
                content_type = ContentType.objects.get_for_model(obj)
                ip_address = _get_client_ip(request)
                
                AdminAction.objects.create(
                    admin_user=request.user,
                    action_type=action_type,
                    content_type=content_type,
                    object_id=obj.pk,
                    object_repr=str(obj)[:200],
                    changes=changes,
                    ip_hash=hash_ip(ip_address)
                )
            except Exception as e:
                logger.error(f"Failed to log admin action: {e}", exc_info=True)
            
            return result
        
        def patched_delete_model(self, request, obj):
            """Patched delete_model to log admin actions."""
            # Store info before deletion
            content_type = ContentType.objects.get_for_model(obj)
            object_id = obj.pk
            object_repr = str(obj)[:200]
            
            # Call original method
            result = original_delete_model(self, request, obj)
            
            # Log action
            try:
                ip_address = _get_client_ip(request)
                
                AdminAction.objects.create(
                    admin_user=request.user,
                    action_type='delete',
                    content_type=content_type,
                    object_id=object_id,
                    object_repr=object_repr,
                    changes={},
                    ip_hash=hash_ip(ip_address)
                )
            except Exception as e:
                logger.error(f"Failed to log admin delete action: {e}", exc_info=True)
            
            return result
        
        # Apply patches
        ModelAdmin.save_model = patched_save_model
        ModelAdmin.delete_model = patched_delete_model


# Thread-local storage for request context
import threading
_thread_local = threading.local()


def _get_client_ip(request):
    """Extract client IP address from request."""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0].strip()
    else:
        ip = request.META.get('REMOTE_ADDR', '')
    return ip


class CacheStatsMiddleware:
    """
    Middleware to track cache hit/miss statistics.
    This is a simplified implementation - production would need cache backend patching.
    """
    
    def __init__(self, get_response):
        self.get_response = get_response
        self.enabled = getattr(settings, 'MONITORING_ENABLED', True)
    
    def __call__(self, request):
        if not self.enabled:
            return self.get_response(request)
        
        # Initialize cache stats for this request
        if not hasattr(cache, '_hits'):
            cache._hits = 0
        if not hasattr(cache, '_misses'):
            cache._misses = 0
        
        response = self.get_response(request)
        return response

