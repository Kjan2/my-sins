"""Extension points only: no model, network calls or automatic approved grades."""
from typing import Protocol
from dataclasses import dataclass


@dataclass(frozen=True)
class GradeSuggestion:
    score: int
    feedback: str
    source: str


class QuestionProvider(Protocol):
    def questions(self, assessment) -> list[dict]: ...


class AnswerEvaluator(Protocol):
    def suggest(self, question: dict, answer: str) -> GradeSuggestion: ...


class TeacherQuestionProvider:
    def questions(self, assessment):
        return list(assessment.questions.values("id", "text", "max_score", "problem_id", "source"))
