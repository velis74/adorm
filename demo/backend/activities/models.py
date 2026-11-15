"""Activities a club runs and their attendance."""

from django.core.validators import MinValueValidator
from django.db import models


class Kind(models.TextChoices):
    TRAINING = "training"
    COMPETITION = "competition"
    SOCIAL = "social"


class AttendanceStatus(models.TextChoices):
    REGISTERED = "registered"
    ATTENDED = "attended"
    CANCELLED = "cancelled"


class Tag(models.Model):
    name = models.CharField(max_length=40, unique=True)

    def __str__(self) -> str:
        return self.name


class Activity(models.Model):
    club = models.ForeignKey("clubs.Club", on_delete=models.CASCADE, related_name="activities")
    coach = models.ForeignKey(
        "clubs.Member", on_delete=models.SET_NULL, null=True, blank=True, related_name="coached_activities"
    )
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.TRAINING)
    starts_at = models.DateTimeField()
    duration = models.DurationField()
    capacity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    price = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    tags = models.ManyToManyField(Tag, blank=True, related_name="activities")
    participants = models.ManyToManyField("clubs.Member", through="Attendance", related_name="activities")

    class Meta:
        verbose_name_plural = "activities"

    def __str__(self) -> str:
        return self.name


class Competition(Activity):
    """An activity with results: a multi-table inheritance child of `Activity`."""

    discipline = models.CharField(max_length=60)
    entry_deadline = models.DateField(null=True, blank=True)


class Attendance(models.Model):
    activity = models.ForeignKey(Activity, on_delete=models.CASCADE, related_name="attendances")
    member = models.ForeignKey("clubs.Member", on_delete=models.CASCADE, related_name="attendances")
    status = models.CharField(max_length=20, choices=AttendanceStatus.choices, default=AttendanceStatus.REGISTERED)
    checked_in_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["activity", "member"], name="activities_attendance_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.member} at {self.activity}"
