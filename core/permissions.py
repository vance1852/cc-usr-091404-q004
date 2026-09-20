"""角色权限：协调员 / 分析员 / 质量人员(QA)。"""
from rest_framework.permissions import BasePermission

from .models import Role


def _role(user) -> str:
    return getattr(user.profile, "role", "") if hasattr(user, "profile") else ""


class IsCoordinator(BasePermission):
    message = "仅协调员可执行此操作。"

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated
                    and (_role(request.user) == Role.COORDINATOR or request.user.is_superuser))


class IsAnalyst(BasePermission):
    message = "仅分析员可执行此操作。"

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated
                    and (_role(request.user) == Role.ANALYST or request.user.is_superuser))


class IsQA(BasePermission):
    message = "仅质量人员可执行此操作。"

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated
                    and (_role(request.user) == Role.QA or request.user.is_superuser))


class IsCoordinatorOrQA(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and (
            _role(request.user) in (Role.COORDINATOR, Role.QA) or request.user.is_superuser
        ))
