from django.http import JsonResponse
from django.urls import path


def healthz(request):
    return JsonResponse(
        {
            "status": "ok",
            "host": request.get_host(),
            "secure": request.is_secure(),
            "forwarded_proto": request.META.get("HTTP_X_FORWARDED_PROTO"),
        }
    )


urlpatterns = [path("healthz", healthz)]
