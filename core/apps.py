from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    verbose_name = "稳定性试验编排"

    def ready(self):
        from . import signals  # noqa: F401
