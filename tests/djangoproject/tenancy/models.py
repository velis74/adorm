"""Organisations with a schema each, people shared by all of them, and the teams and enrolments in each schema."""

from django.db import models

from adorm.contrib.django import TenantRelation


class Organisation(models.Model):
    name = models.CharField(max_length=100)
    schema_name = models.CharField(max_length=63, unique=True)


class Person(models.Model):
    name = models.CharField(max_length=100)
    enrolments = TenantRelation(
        "tenancy.Enrolment",
        field="person",
        schema="organisations__organisation__schema_name",
        tenant_models=["tenancy.Team", "tenancy.Enrolment"],
    )


class PersonOrganisation(models.Model):
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="organisations")
    organisation = models.ForeignKey(Organisation, on_delete=models.CASCADE, related_name="people")


class Team(models.Model):
    """A team of an organisation, in the organisation's schema."""

    name = models.CharField(max_length=100, unique=True)


class Enrolment(models.Model):
    """A person's enrolment in an organisation, in the organisation's schema."""

    person = models.ForeignKey(Person, on_delete=models.CASCADE)
    team = models.ForeignKey(Team, on_delete=models.CASCADE, null=True)
    role = models.CharField(max_length=20)
    joined = models.DateField()
    fee = models.DecimalField(max_digits=8, decimal_places=2)
