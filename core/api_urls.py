from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import api

router = DefaultRouter()
router.register("storage-conditions", api.StorageConditionViewSet)
router.register("chambers", api.ChamberViewSet)
router.register("products", api.ProductViewSet)
router.register("assays", api.AssayViewSet)
router.register("protocols", api.ProtocolViewSet)
router.register("protocol-versions", api.ProtocolVersionViewSet)
router.register("timepoints", api.TimePointViewSet)
router.register("batches", api.BatchViewSet)
router.register("samples", api.SampleViewSet)
router.register("actions", api.ActionViewSet, basename="action")
router.register("events", api.EnvironmentEventViewSet)
router.register("exposures", api.ExposureViewSet, basename="exposure")
router.register("results", api.AssayResultViewSet, basename="result")

urlpatterns = [
    path("me/", api.MeView.as_view(), name="me"),
    path("actions-overdue/", api.OverdueView.as_view(), name="actions-overdue"),
    path("trend-dataset/", api.TrendDatasetView.as_view(), name="trend-dataset"),
    path("", include(router.urls)),
]
