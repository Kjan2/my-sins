import csv
import io
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from .services import teacher
from .models import Course
from .permissions import require_course


@transaction.atomic
def import_students(actor, content, course_id):
    teacher(actor)
    require_course(actor, course_id)
    course = Course.objects.get(pk=course_id)
    if len(content) > 1048576:
        raise ValidationError("CSV должен быть не больше 1 МиБ.")
    try:
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")), strict=True)
        fields = reader.fieldnames or []
        allowed = {"username", "password", "first_name", "last_name", "email", "group"}
        if not {"username", "password", "group"}.issubset(fields) or len(fields) != len(set(fields)) or set(fields) - allowed:
            raise ValidationError("Нужны уникальные колонки username,password,group; дополнительно first_name,last_name,email.")
        rows = []
        names = set()
        for number, row in enumerate(reader, 2):
            if len(rows) >= 200:
                raise ValidationError("За один раз можно импортировать не больше 200 студентов.")
            if None in row or any(value is None for value in row.values()):
                raise ValidationError(f"Строка {number}: число значений не соответствует заголовку.")
            username, group = row["username"].strip(), row["group"].strip()
            if Group.objects.filter(name=group).exclude(courses=course).exists() and not actor.is_superuser:
                raise ValidationError(f"Строка {number}: группа недоступна в этом курсе. Используйте другое название или обратитесь к администратору.")
            if not username or not group or len(group) > 150 or username in names:
                raise ValidationError(f"Строка {number}: укажите уникальный логин и группу длиной до 150 символов.")
            if get_user_model().objects.filter(username=username).exists():
                raise ValidationError(f"Строка {number}: логин уже существует. Импорт создаёт только новых студентов.")
            user = get_user_model()(username=username, **{key: row.get(key, "").strip() for key in ("first_name", "last_name", "email")})
            user.set_unusable_password()
            try:
                user.full_clean()
                validate_password(row["password"], user)
            except ValidationError as exc:
                raise ValidationError(f"Строка {number}: {' '.join(exc.messages)}") from exc
            if user.username in names:
                raise ValidationError(f"Строка {number}: логины совпадают после нормализации Unicode.")
            names.add(user.username)
            rows.append((user, group, row["password"]))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ValidationError("Не удалось прочитать CSV UTF-8 с разделителем-запятой.") from exc
    if not rows:
        raise ValidationError("CSV не содержит студентов.")
    for user, name, password in rows:
        user.set_password(password)
        user.save()
        group, _ = Group.objects.get_or_create(name=name)
        course.groups.add(group)
        user.groups.add(group)
    return len(rows)
