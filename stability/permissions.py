"""
Role-based permissions.

Three operational roles (Django groups), plus Django staff/superusers who hold
every role for administration:

* ``coordinator`` 协调员：方案/批次/计划、样品分配与替代、取样登记、
  异常与环境事件登记。
* ``analyst``     分析员：仅提交检测结果。
* ``qa``          质量人员：环境事件与样品异常的影响评估、检测结果的
  纳入 / 排除 / 追加考察。

Reads are available to every authenticated user.  The role of a user never
changes the data model — a coordinator cannot decide QA status even by
calling the API directly, and an analyst cannot create substitutions.
"""
from __future__ import annotations

import functools
import json

from django.http import HttpRequest, JsonResponse

ROLE_COORDINATOR = "coordinator"
ROLE_ANALYST = "analyst"
ROLE_QA = "qa"

ROLE_LABELS = {
    ROLE_COORDINATOR: "协调员",
    ROLE_ANALYST: "分析员",
    ROLE_QA: "质量人员",
}


def user_roles(user) -> set[str]:
    if not user or not user.is_authenticated:
        return set()
    roles = set(user.groups.filter(name__in=ROLE_LABELS).values_list("name", flat=True))
    if user.is_staff or user.is_superuser:
        roles.update(ROLE_LABELS)
    return roles


def has_role(user, *roles: str) -> bool:
    return bool(set(roles) & user_roles(user))


def require_roles(*roles: str):
    """JSON API decorator: 401 unauthenticated, 403 wrong role."""
    def decorator(view_func):
        @functools.wraps(view_func)
        def wrapper(request: HttpRequest, *args, **kwargs):
            if not request.user.is_authenticated:
                return JsonResponse(
                    {"error": "未认证", "detail": "请先登录"},
                    status=401,
                )
            if not has_role(request.user, *roles):
                return JsonResponse(
                    {
                        "error": "权限不足",
                        "detail": (
                            f"该操作需要角色：{'/'.join(ROLE_LABELS[r] for r in roles)}"
                        ),
                        "your_roles": sorted(user_roles(request.user)),
                    },
                    status=403,
                )
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator


def read_access(view_func):
    """Any authenticated user may read."""
    @functools.wraps(view_func)
    def wrapper(request: HttpRequest, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "未认证", "detail": "请先登录"}, status=401)
        return view_func(request, *args, **kwargs)
    return wrapper


def parse_body(request: HttpRequest) -> dict:
    """Parse a JSON request body; raises ValueError with a Chinese message."""
    if request.content_type and "application/json" in request.content_type:
        try:
            data = json.loads(request.body.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}")
    else:
        data = {}
        for key, value in request.POST.items():
            data[key] = value
    if not isinstance(data, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return data
