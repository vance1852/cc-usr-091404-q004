from django.urls import path

from . import views

urlpatterns = [
    path("", views.dataset_page, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("batches/<int:batch_id>/", views.batch_detail, name="batch-detail"),
    path("events/", views.events_page, name="events"),
    path("dataset/", views.dataset_page, name="dataset-page"),
]
