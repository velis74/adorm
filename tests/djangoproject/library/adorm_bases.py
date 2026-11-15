"""Hand-written adorm bases of the ``library`` app's generated manager and query set classes."""

import datetime

from adorm import Manager, QuerySet


class LoanQuerySetBase(QuerySet):
    """Client-side methods of the loans' query set."""

    def overdue(self, today: datetime.date) -> "LoanQuerySetBase":
        """Loans due before ``today``."""
        return self.filter(due__lt=today)


class VisibleManagerBase(Manager):
    """A manager whose ``get_queryset()`` runs in the client."""

    def get_queryset(self) -> QuerySet:
        return super().get_queryset().filter(id__gt=0)
