"""Shared, atomic login limits for the public and administrative login forms."""
from datetime import timedelta
import math
from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.deprecation import MiddlewareMixin
from .models import LoginRateBucket


class LoginLimitMiddleware(MiddlewareMixin):
    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.method != "POST" or request.path not in ("/accounts/login/", "/admin/login/"):
            return
        username = request.POST.get("username", "").strip()[:150]
        address = request.META.get("REMOTE_ADDR", "unknown")
        if settings.LOGIN_TRUST_PROXY:
            address = request.META.get("HTTP_X_REAL_IP", address)
        account_key = salted_hmac("login-account", username, algorithm="sha256").hexdigest()
        address_key = salted_hmac("login-address", address, algorithm="sha256").hexdigest()
        now = timezone.now()
        with transaction.atomic():
            LoginRateBucket.objects.filter(expires_at__lte=now).delete()
            buckets = []
            for key, limit in ((address_key, settings.LOGIN_IP_LIMIT), (account_key, settings.LOGIN_ACCOUNT_LIMIT)):
                bucket, _ = LoginRateBucket.objects.get_or_create(key=key, defaults={
                    "expires_at": now + timedelta(seconds=settings.LOGIN_WINDOW_SECONDS)})
                if bucket.attempts >= limit:
                    seconds = max(1, math.ceil((bucket.expires_at - now).total_seconds()))
                    response = HttpResponse("Слишком много попыток входа. Повторите позже.", status=429,
                                            content_type="text/plain; charset=utf-8")
                    response["Retry-After"] = str(seconds)
                    return response
                buckets.append(bucket)
            for bucket in buckets:
                bucket.attempts += 1
                bucket.save(update_fields=["attempts"])
        request.login_account_key = account_key

    def process_response(self, request, response):
        # A successful login resets the account counter, but not the shared IP counter.
        key = getattr(request, "login_account_key", None)
        if key and response.status_code == 302 and request.user.is_authenticated:
            LoginRateBucket.objects.filter(key=key).delete()
        return response
