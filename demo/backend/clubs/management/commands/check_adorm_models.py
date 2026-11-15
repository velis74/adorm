"""``manage.py check_adorm_models``: compare ``demo.api.models`` with the Django models; exit 1 on a difference."""

import sys

from django.core.management.base import BaseCommand

from demo.api.models import MODELS
from demo.backend.model_check import compare


class Command(BaseCommand):
    help = "Compare the demo's adorm models with the Django models."

    def handle(self, *args, **options):  # noqa: ARG002 - Django's signature
        problems = [problem for model in MODELS for problem in compare(model)]
        for problem in problems:
            self.stderr.write(problem)
        if problems:
            sys.exit(1)
        self.stdout.write(self.style.SUCCESS(f"{len(MODELS)} adorm models match the Django models."))
