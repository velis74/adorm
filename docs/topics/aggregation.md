# Aggregation

An aggregate computes one value from many rows: a count, a sum, an average, a minimum or a maximum. adorm computes
aggregates the way Django does: over all rows of a query (`aggregate()`), per row (`annotate()` on model rows) or per
group of rows (`values()` followed by `annotate()`). The [Django driver](/topics/django-driver) and the
[native PostgreSQL driver](/topics/postgresql-driver) run every form on this page.

The examples use the demo's models:

```python
from decimal import Decimal

from adorm import Avg, Case, Coalesce, Count, Exists, F, Max, Min, OuterRef, Q, Subquery, Sum, TruncMonth, Value, When
from demo.models.billing import Invoice
from demo.models.clubs import Club, Member, Membership
```

## Over all rows: `aggregate()`

`aggregate()` returns a dict with one value per aggregate. A keyword argument names its value; an aggregate of one
field passed positionally is named `<field>__<aggregate>`:

```python
await Membership.objects.aggregate(Sum("fee"), rows=Count("*"))
# {"fee__sum": Decimal("1102.00"), "rows": 13}

await Membership.objects.filter(active=True).aggregate(cheapest=Min("fee"), dearest=Max("fee"))
```

The value of an expression may combine aggregates:

```python
await Membership.objects.aggregate(spread=Max("fee") - Min("fee"))
```

Over no rows `Count` gives `0`, the other aggregates `None`, or their `default`:

```python
await Membership.objects.filter(role="treasurer").aggregate(n=Count("*"), total=Sum("fee", default=0))
# {"n": 0, "total": Decimal("0")}
```

`aggregate()` is a terminal method: it runs one [`Aggregate`](/ref/operations#adorm-operations-aggregate-query-aggregations)
operation and returns its result. It aggregates the rows the query returns, so a slice or `distinct()` applies
first:

```python
await Membership.objects.order_by("-fee")[:3].aggregate(Sum("fee"))   # the three highest fees
```

## Per row: `annotate()`

On model rows, an annotation with an aggregate is computed for each row over its related rows:

```python
clubs = Club.objects.annotate(headcount=Count("memberships"), fees=Sum("memberships__fee"))
async for club in clubs.order_by("-headcount"):
    print(club.name, club.headcount, club.fees)
```

The annotation is a value of the instance (an extra field); a condition and an ordering refer to it by name. A
condition on an aggregate filters the rows after they are aggregated (SQL `HAVING`):

```python
await Club.objects.annotate(headcount=Count("memberships")).filter(headcount__gt=3).count()
```

An aggregate over annotated rows aggregates the annotations:

```python
await Club.objects.annotate(headcount=Count("memberships")).aggregate(Avg("headcount"))
# {"headcount__avg": 4.333333333333333}
```

## Per group: `values()` and `annotate()`

`values()` followed by an annotation with an aggregate groups the rows by the selected names. Each row of the result
is one group:

```python
roles = Membership.objects.values("role").annotate(n=Count("*"), fees=Sum("fee")).order_by("role")
[row async for row in roles]
# [{"role": "board", "n": 1, "fees": Decimal("60.00")},
#  {"role": "coach", "n": 3, "fees": Decimal("0.00")},
#  {"role": "member", "n": 9, "fees": Decimal("1042.00")}]
```

The order of the calls decides: `annotate()` before `values()` aggregates per row, `annotate()` after `values()`
per group. An annotation without an aggregate made before `values()` can be a grouping column, which groups by a
computed value, such as the month of a date:

```python
months = (
    Invoice.objects.annotate(month=TruncMonth("issued"))
    .values("month")
    .annotate(n=Count("pk"), total=Sum("total"))
    .order_by("month")
)
[row async for row in months.values_list("month", "n", "total")]
# [(date(2026, 1, 1), 10, Decimal("1102.00")), (date(2026, 3, 1), 9, Decimal("165.00"))]
```

A grouped query is an ordinary query: `filter()` on an aggregate keeps some groups, `count()` counts the groups and
`aggregate()` aggregates them:

```python
groups = Membership.objects.values("club_id").annotate(n=Count("*"))
await groups.filter(n__gte=5).count()                 # 2
await groups.aggregate(largest=Max("n"))              # {"largest": 5}
```

`first()` and `last()` on a grouped query need `order_by()`; ordering by the primary key would split the groups.
[`Query.group_by`](/ref/query#annotate-annotations) shows the grouping names. An annotation without an aggregate added
after the grouping is selected and joins the grouping names, as in Django:

```python
Membership.objects.values("role").annotate(n=Count("*")).annotate(club=F("club__slug"))
# one row per role and club
```

## Conditional aggregation

`filter=` limits the rows one aggregate counts, so several aggregates over different rows run in one query:

```python
await Membership.objects.aggregate(
    paying=Count("pk", filter=Q(fee__gt=0)),
    coach_fees=Sum("fee", filter=Q(role="coach"), default=0),
)

Club.objects.annotate(inactive=Count("memberships", filter=Q(memberships__active=False)))
```

The order of `filter()` and `annotate()` decides, as in Django. An aggregate annotation added after a `filter()` on a
multi-valued relation counts only the related rows the condition matched. A `filter()` added after the annotation joins
the relation again: each row repeats once per related row the condition matches, which multiplies the aggregate.
`filter=` limits one aggregate whatever the order:

```python
Club.objects.filter(memberships__role="member").annotate(n=Count("memberships"))   # the members of each club
Club.objects.annotate(n=Count("memberships")).filter(memberships__role="member")   # memberships × members
```

`Case` and `When` compute a value per row that an aggregate can then sum:

```python
await Membership.objects.aggregate(discounted=Sum(Case(When(role="board", then=F("fee") / 2), default=F("fee"))))
# board fees count half: 1072
```

## Aliases

`alias()` names an expression that conditions, orderings and other expressions use without returning it in the
rows. It saves selecting a value nobody reads:

```python
busy = Club.objects.alias(headcount=Count("memberships")).filter(headcount__gt=3).order_by("-headcount")
[club.slug async for club in busy]          # the clubs; club.headcount does not exist
```

`values()` of an alias raises `ValueError`; `annotate(shown=F("alias_name"))` selects its value under another name.

## Subqueries with aggregates

`Subquery` and `OuterRef` compute an aggregate per row from rows the outer query does not join, which keeps two
aggregates over different relations from multiplying each other's rows:

```python
billed = (
    Invoice.objects.filter(member_id=OuterRef("pk"))
    .values("member_id")
    .annotate(total=Sum("total"))
    .values("total")
)
members = Member.objects.annotate(billed=Coalesce(Subquery(billed), Value(Decimal("0"))))
```

`OuterRef("pk")` is the primary key of the outer query's row; in a subquery nested in a subquery,
`OuterRef(OuterRef("pk"))` refers to the query one level further out. `Exists` tests for a related row:

```python
unpaid = Invoice.objects.filter(member_id=OuterRef("pk"), status="open")
Member.objects.filter(Exists(unpaid))
```

## Porting Django code

Django code that aggregates moves to adorm with the same calls and `await`. Two kinds of code need a new home.

### Custom query set methods

A method of a Django query set that builds a query becomes a function over an adorm query set, or a client-side
method of an adorm `QuerySet` subclass ([managers](/topics/managers#query-set-classes)). Building runs in the client
and needs no connection; the function returns a query set that further calls extend:

```python
# Django
class ClubQuerySet(models.QuerySet):
    def with_headcount(self):
        return self.annotate(headcount=Count("memberships", filter=Q(memberships__active=True)))


# adorm: a function over any query set of clubs
def with_headcount(clubs):
    return clubs.annotate(headcount=Count("memberships", filter=Q(memberships__active=True)))


busy = await with_headcount(Club.objects.filter(city="Ljubljana")).filter(headcount__gt=3).count()
```

For a generated model, the method goes into the class that
[`@client_base`](/topics/managers#client-side-methods-of-generated-models) names, so
`Club.objects.with_headcount()` keeps working.

### Model properties that read the database

A Django property such as `club.headcount` that runs a query on every access becomes either an annotation of the
query that loads the rows, so the value comes with the row in the same statement, or an async function that runs the
query for one instance:

```python
# Django
class Club(models.Model):
    @property
    def fees(self):
        return self.memberships.aggregate(total=Sum("fee"))["total"] or 0


# adorm, as an annotation: one statement for every club
clubs = Club.objects.annotate(fees=Coalesce(Sum("memberships__fee"), Value(Decimal("0"))))


# adorm, as a ported function: one operation per call
async def club_fees(club):
    return (await club.memberships.aggregate(total=Sum("fee", default=0)))["total"]
```

The annotation suits lists, where a property would run one query per row; the function suits a single instance at
hand. Neither runs implicitly on attribute access: reading the database is always awaited.
