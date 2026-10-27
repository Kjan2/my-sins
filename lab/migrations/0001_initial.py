from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    initial = True
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='Problem', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('title', models.CharField('Название', max_length=200)),
            ('statement', models.TextField('Условие (обычный текст)')),
            ('time_limit', models.FloatField('Лимит секунд', default=2, validators=[MinValueValidator(0.1), MaxValueValidator(10)])),
            ('memory_mb', models.PositiveIntegerField('Память МБ', default=128, validators=[MinValueValidator(32), MaxValueValidator(512)]))
        ], options={}),
        migrations.CreateModel(name='TestCase', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('problem', models.ForeignKey('lab.problem', on_delete=models.CASCADE, related_name='tests')),
            ('label', models.CharField('Название', max_length=100)),
            ('input', models.TextField('Ввод', blank=True, max_length=65536)),
            ('expected', models.TextField('Ожидаемый вывод', blank=True, max_length=65536)),
            ('is_sample', models.BooleanField('Открытый пример', default=False)),
            ('order', models.PositiveIntegerField(default=0))
        ], options={'ordering': ['order', 'pk']}),
        migrations.CreateModel(name='Assessment', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('title', models.CharField('Название', max_length=200)),
            ('kind', models.CharField('Тип', choices=[('lab', 'Лабораторная'), ('exam', 'Зачёт')], default='lab', max_length=8)),
            ('instructions', models.TextField('Инструкция', blank=True)),
            ('opens_at', models.DateTimeField('Открытие')),
            ('closes_at', models.DateTimeField('Закрытие')),
            ('duration_minutes', models.PositiveIntegerField('Продолжительность, минут', null=True, blank=True, validators=[MinValueValidator(1)])),
            ('pass_score', models.PositiveIntegerField('Проходной балл', default=1)),
            ('submission_limit', models.PositiveIntegerField('Лимит оцениваемых отправок на задачу', null=True, blank=True, validators=[MinValueValidator(1)])),
            ('reveal_after_close', models.BooleanField('Раскрывать оценки после закрытия', default=False))
        ], options={}),
        migrations.CreateModel(name='AssessmentProblem', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('assessment', models.ForeignKey('lab.assessment', on_delete=models.CASCADE, related_name='problem_rules')),
            ('problem', models.ForeignKey('lab.problem', on_delete=models.PROTECT, verbose_name='Задача')),
            ('max_score', models.PositiveIntegerField('Максимум баллов', default=10, validators=[MinValueValidator(1)])),
            ('required', models.BooleanField('Обязательная', default=True)),
            ('order', models.PositiveIntegerField('Порядок', default=0))
        ], options={'ordering': ['order', 'pk'], 'constraints': [models.UniqueConstraint(fields=['assessment', 'problem'], name='unique_assessment_problem')]}),
        migrations.CreateModel(name='TheoryQuestion', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('assessment', models.ForeignKey('lab.assessment', on_delete=models.CASCADE, related_name='questions')),
            ('problem', models.ForeignKey('lab.problem', null=True, blank=True, on_delete=models.PROTECT, verbose_name='Связанная задача')),
            ('text', models.TextField('Вопрос')),
            ('max_score', models.PositiveIntegerField('Максимум баллов', default=5, validators=[MinValueValidator(1)])),
            ('source', models.CharField('Источник', max_length=100, default='teacher'))
        ], options={}),
        migrations.CreateModel(name='AssessmentVersion', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('assessment', models.ForeignKey('lab.assessment', on_delete=models.PROTECT, related_name='versions')),
            ('number', models.PositiveIntegerField()),
            ('snapshot', models.JSONField()),
            ('published_at', models.DateTimeField(auto_now_add=True)),
            ('published_by', models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT))
        ], options={'constraints': [models.UniqueConstraint(fields=['assessment', 'number'], name='unique_assessment_version')]}),
        migrations.CreateModel(name='Attempt', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('student', models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)),
            ('assessment', models.ForeignKey('lab.assessment', on_delete=models.PROTECT, related_name='attempts')),
            ('version', models.ForeignKey('lab.assessmentversion', on_delete=models.PROTECT)),
            ('started_at', models.DateTimeField()),
            ('deadline', models.DateTimeField()),
            ('finished_at', models.DateTimeField(null=True, blank=True))
        ], options={'constraints': [models.UniqueConstraint(fields=['student', 'assessment'], name='one_attempt_per_student')]}),
        migrations.CreateModel(name='DeadlineExtension', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('attempt', models.ForeignKey('lab.attempt', on_delete=models.PROTECT, related_name='extensions')),
            ('actor', models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)),
            ('old_deadline', models.DateTimeField()),
            ('new_deadline', models.DateTimeField()),
            ('reason', models.TextField()),
            ('created_at', models.DateTimeField(auto_now_add=True))
        ], options={}),
        migrations.CreateModel(name='Draft', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('attempt', models.ForeignKey('lab.attempt', on_delete=models.CASCADE)),
            ('problem_id', models.PositiveBigIntegerField()),
            ('source', models.TextField(max_length=65536, blank=True)),
            ('updated_at', models.DateTimeField(auto_now=True))
        ], options={'constraints': [models.UniqueConstraint(fields=['attempt', 'problem_id'], name='one_draft_per_problem')]}),
        migrations.CreateModel(name='Submission', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('attempt', models.ForeignKey('lab.attempt', on_delete=models.PROTECT, related_name='submissions')),
            ('problem_id', models.PositiveBigIntegerField()),
            ('source', models.TextField(max_length=65536)),
            ('kind', models.CharField(choices=[('sample', 'Примеры'), ('judge', 'На проверку')], max_length=8)),
            ('created_at', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
            ('started_at', models.DateTimeField(null=True, blank=True)),
            ('finished_at', models.DateTimeField(null=True, blank=True)),
            ('status', models.CharField(choices=[('queued', 'В очереди'), ('running', 'Выполняется'), ('accepted', 'Принято'), ('wrong_answer', 'Неверный ответ'), ('compile_error', 'Ошибка компиляции'), ('runtime_error', 'Ошибка выполнения'), ('limit_exceeded', 'Превышение лимита'), ('infra_error', 'Ошибка инфраструктуры')], max_length=20, default='queued', db_index=True)),
            ('result', models.JSONField(default=dict, blank=True))
        ], options={}),
        migrations.CreateModel(name='TheoryAnswer', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('attempt', models.ForeignKey('lab.attempt', on_delete=models.PROTECT, related_name='answers')),
            ('question_id', models.PositiveBigIntegerField()),
            ('text', models.TextField('Ответ', blank=True, max_length=20000)),
            ('submission', models.ForeignKey('lab.submission', null=True, blank=True, on_delete=models.PROTECT, verbose_name='Версия кода')),
            ('updated_at', models.DateTimeField(auto_now=True)),
            ('score', models.PositiveIntegerField('Утверждённые баллы', null=True, blank=True)),
            ('feedback', models.TextField('Комментарий преподавателя', blank=True)),
            ('proposed_score', models.PositiveIntegerField('Предложенные баллы', null=True, blank=True, editable=False)),
            ('proposed_feedback', models.TextField(blank=True, editable=False)),
            ('graded_by', models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)),
            ('graded_at', models.DateTimeField(null=True, blank=True))
        ], options={'constraints': [models.UniqueConstraint(fields=['attempt', 'question_id'], name='one_answer_per_question')]}),
        migrations.AddField(model_name='assessment', name='groups', field=models.ManyToManyField('auth.Group', verbose_name='Учебные группы')),
        migrations.AddField(model_name='assessment', name='problems', field=models.ManyToManyField('lab.problem', through='lab.AssessmentProblem', verbose_name='Задачи')),
        migrations.AddField(model_name='assessment', name='current_version', field=models.ForeignKey('lab.AssessmentVersion', null=True, blank=True, on_delete=models.PROTECT, related_name='+', editable=False)),
    ]
