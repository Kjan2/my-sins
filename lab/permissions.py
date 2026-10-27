"""Course access shared by HTTP views, admin forms and service operations."""
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from .models import Course


def courses_for(user):
    courses = Course.objects.all()
    if not user.is_active or not user.is_staff:
        return courses.none()
    return courses if user.is_superuser else courses.filter(teachers=user)


def scoped(user, queryset, course_lookup="course"):
    if user.is_active and user.is_superuser:
        return queryset
    return queryset.filter(**{f"{course_lookup}__in": courses_for(user)}).distinct()


def can_manage_course(user, course_id):
    return courses_for(user).filter(pk=course_id).exists()


def require_course(user, course_id):
    if not can_manage_course(user, course_id):
        raise PermissionDenied


def groups_for(user):
    groups = Group.objects.all()
    return groups if user.is_active and user.is_superuser else scoped(user, groups, "courses")
