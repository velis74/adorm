"""The Django ORM driver.

`DjangoDriver` executes adorm operations with Django's synchronous ORM on a thread per connection;
`adorm.drivers.django.translation` turns adorm queries into Django query sets. Importing this package imports Django.
"""

from adorm.drivers.django.driver import (
    adopt_context_locals,
    CAPABILITIES,
    DjangoConnection,
    DjangoDriver,
    map_django_error,
)

__all__ = ["CAPABILITIES", "DjangoConnection", "DjangoDriver", "adopt_context_locals", "map_django_error"]
