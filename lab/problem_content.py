"""One renderer for the frozen student statement and the teacher preview."""
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from markdown_it import MarkdownIt

MARKDOWN = MarkdownIt("commonmark", {"html": False}).disable("image")


def render_text(text, format="plain"):
    if format == "markdown":
        # HTML and images are disabled; the parser validates link protocols.
        return mark_safe(MARKDOWN.render(text))
    return format_html('<div class="plain">{}</div>', text)


def problem_sections(problem):
    format = problem.get("statement_format", "plain")
    sections = [("", render_text(problem["statement"], format))]
    for key, title in (("input_description", "Входные данные"),
                       ("output_description", "Выходные данные"), ("constraints", "Ограничения")):
        if problem.get(key):
            sections.append((title, render_text(problem[key], format)))
    return sections
