from django.contrib import admin
from django.urls import include, path
from django.views.generic import TemplateView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/", include("rest_framework.urls")),
    path("api/", include("core.api_urls")),
    path("", TemplateView.as_view(template_name="dashboard.html"), name="dashboard"),
]
