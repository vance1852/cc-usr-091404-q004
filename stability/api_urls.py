from django.urls import path

from . import api_views

urlpatterns = [
    path("me/", api_views.me, name="api-me"),

    # protocols
    path("protocols/", api_views.protocol_list, name="api-protocol-list"),
    path("protocols/create/", api_views.protocol_create, name="api-protocol-create"),

    # chambers / events
    path("chambers/", api_views.chamber_list, name="api-chamber-list"),
    path("events/", api_views.event_list, name="api-event-list"),
    path("events/<int:event_id>/assess/", api_views.event_assess,
         name="api-event-assess"),

    # batches
    path("batches/", api_views.batch_list, name="api-batch-list"),
    path("batches/<int:batch_id>/", api_views.batch_detail, name="api-batch-detail"),
    path("batches/<int:batch_id>/schedule/", api_views.batch_schedule_view,
         name="api-batch-schedule"),
    path("batches/<int:batch_id>/overdue/", api_views.batch_overdue_view,
         name="api-batch-overdue"),
    path("batches/<int:batch_id>/timeline/", api_views.batch_timeline_view,
         name="api-batch-timeline"),
    path("batches/<int:batch_id>/exposure/", api_views.batch_exposure_view,
         name="api-batch-exposure"),
    path("batches/<int:batch_id>/generate-plan/", api_views.batch_generate_plan,
         name="api-batch-generate-plan"),

    # actions / assignments
    path("actions/<int:action_id>/", api_views.action_detail,
         name="api-action-detail"),
    path("actions/<int:action_id>/assign/", api_views.action_assign,
         name="api-action-assign"),
    path("actions/<int:action_id>/substitute/", api_views.action_substitute,
         name="api-action-substitute"),
    path("assignments/<int:assignment_id>/withdraw/",
         api_views.assignment_withdraw, name="api-assignment-withdraw"),

    # sample exceptions
    path("samples/<int:sample_id>/exceptions/", api_views.sample_exception,
         name="api-sample-exception"),
    path("exceptions/<int:exception_id>/assess/",
         api_views.exception_assess, name="api-exception-assess"),

    # results
    path("assignments/<int:assignment_id>/results/", api_views.result_submit,
         name="api-result-submit"),
    path("results/<int:result_id>/decision/", api_views.result_decide,
         name="api-result-decide"),
    path("results/", api_views.result_list, name="api-result-list"),

    # dataset
    path("dataset/", api_views.dataset, name="api-dataset"),
]
