from django.db import OperationalError
from django.http import JsonResponse
from django.utils.deprecation import MiddlewareMixin


class ServiceMiddleware(MiddlewareMixin):
    def process_exception(self, request, exception):
        if isinstance(exception, OperationalError) and "locked" in str(exception).lower():
            response = JsonResponse({"error": "База занята. Повторите сохранение через несколько секунд."}, status=503)
            response["Retry-After"] = "3"
            return response

    def process_response(self, request, response):
        if not request.path.startswith("/static/"):
            response["Cache-Control"] = "private, no-store"
        return response
