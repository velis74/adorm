from django.contrib.postgres.operations import CryptoExtension
from django.db import migrations


class Migration(migrations.Migration):
    """``pgcrypto`` on PostgreSQL, for the SHA functions; nothing on another database."""

    dependencies = [("activities", "0001_initial")]

    operations = [CryptoExtension()]
