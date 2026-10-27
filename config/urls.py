from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path
from lab import views
from lab import teacher_views

urlpatterns = [
    path("teacher/", teacher_views.dashboard, name="teacher_dashboard"),
    path("teacher/students/import/", teacher_views.import_students, name="teacher_import_students"),
    path("teacher/assessments/<int:pk>/", teacher_views.gradebook, name="teacher_gradebook"),
    path("teacher/attempts/<int:pk>/", teacher_views.attempt, name="teacher_attempt"),
    path("teacher/judge/", teacher_views.judge, name="teacher_judge"),
    path("teacher/judge/state/", teacher_views.judge_state, name="teacher_judge_state"),
    path("teacher/submissions/<int:pk>/requeue/", teacher_views.requeue, name="teacher_requeue"),
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", views.dashboard, name="dashboard"),
    path("assessments/<int:pk>/", views.rules, name="rules"),
    path("assessments/<int:pk>/start/", views.start, name="start"),
    path("attempts/<int:pk>/", views.attempt, name="attempt"),
    path("attempts/<int:pk>/save/", views.save, name="save"),
    path("attempts/<int:pk>/submit/", views.submit, name="submit"),
    path("attempts/<int:pk>/finish/", views.finish, name="finish"),
    path("attempts/<int:pk>/state/", views.state, name="state"),
    path("attempts/<int:pk>/history/", views.history, name="submission_history"),
    path("attempts/<int:pk>/results/", views.results, name="results"),
    path("submissions/<int:pk>/", views.submission, name="submission"),
    path("submissions/<int:pk>/state/", views.submission_state, name="submission_state"),
]
