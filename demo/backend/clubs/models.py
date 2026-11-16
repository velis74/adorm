"""Clubs and their members."""

from django.contrib.postgres.fields import ArrayField
from django.core.validators import MinValueValidator
from django.db import models


class Level(models.TextChoices):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class Role(models.TextChoices):
    MEMBER = "member"
    COACH = "coach"
    BOARD = "board"


class Club(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    founded = models.DateField()
    city = models.CharField(max_length=100)
    address = models.JSONField(default=dict, blank=True)
    disciplines = ArrayField(models.CharField(max_length=30), default=list, blank=True)
    members = models.ManyToManyField("Member", through="Membership", related_name="clubs")

    def __str__(self) -> str:
        return self.name


class Member(models.Model):
    first_name = models.CharField(max_length=60)
    last_name = models.CharField(max_length=60)
    email = models.EmailField(unique=True)
    birth_date = models.DateField(null=True, blank=True)
    level = models.CharField(max_length=20, choices=Level.choices, default=Level.BEGINNER)
    joined = models.DateTimeField()
    tags = models.JSONField(default=list, blank=True)

    def __str__(self) -> str:
        return f"{self.first_name} {self.last_name}"


class Membership(models.Model):
    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="memberships")
    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    since = models.DateField()
    fee = models.DecimalField(max_digits=8, decimal_places=2, validators=[MinValueValidator(0)])
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["club", "member"], name="clubs_membership_unique")]

    def __str__(self) -> str:
        return f"{self.member} in {self.club}"
