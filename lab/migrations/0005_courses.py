import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def preserve_existing_data(apps, schema_editor):
    alias = schema_editor.connection.alias
    Course = apps.get_model("lab", "Course")
    Problem = apps.get_model("lab", "Problem")
    Assessment = apps.get_model("lab", "Assessment")
    Group = apps.get_model("auth", "Group")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    if not (Problem.objects.using(alias).exists() or Assessment.objects.using(alias).exists()
            or Group.objects.using(alias).exists()):
        return
    course = Course.objects.using(alias).create(title="Общий курс (перенесённые данные)")
    course.teachers.add(*User.objects.using(alias).filter(is_active=True, is_staff=True))
    course.groups.add(*Group.objects.using(alias).all())
    Problem.objects.using(alias).update(course=course)
    Assessment.objects.using(alias).update(course=course)


class Migration(migrations.Migration):
    dependencies = [
        ("lab", "0004_project_improvements"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Course",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=200, verbose_name="Название")),
                ("teachers", models.ManyToManyField(limit_choices_to={"is_staff": True, "is_active": True},
                    related_name="teaching_courses", to=settings.AUTH_USER_MODEL, verbose_name="Преподаватели")),
                ("groups", models.ManyToManyField(blank=True, related_name="courses", to="auth.group", verbose_name="Учебные группы")),
            ],
            options={"verbose_name": "Курс", "verbose_name_plural": "Курсы"},
        ),
        migrations.AddField(model_name="problem", name="course", field=models.ForeignKey(null=True,
            on_delete=django.db.models.deletion.PROTECT, related_name="problems", to="lab.course", verbose_name="Курс")),
        migrations.AddField(model_name="assessment", name="course", field=models.ForeignKey(null=True,
            on_delete=django.db.models.deletion.PROTECT, related_name="assessments", to="lab.course", verbose_name="Курс")),
        migrations.RunPython(preserve_existing_data, migrations.RunPython.noop),
        migrations.AlterField(model_name="problem", name="course", field=models.ForeignKey(
            on_delete=django.db.models.deletion.PROTECT, related_name="problems", to="lab.course", verbose_name="Курс")),
        migrations.AlterField(model_name="assessment", name="course", field=models.ForeignKey(
            on_delete=django.db.models.deletion.PROTECT, related_name="assessments", to="lab.course", verbose_name="Курс")),
    ]
