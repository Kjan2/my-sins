from django.core.exceptions import ValidationError


TEXT_BYTES_LIMIT = 65536


def validate_utf8_size(value):
    try:
        valid = isinstance(value, str) and len(value.encode("utf-8")) <= TEXT_BYTES_LIMIT
    except UnicodeEncodeError:
        valid = False
    if not valid:
        raise ValidationError("Текст должен быть UTF-8 размером не больше 64 КиБ.")
