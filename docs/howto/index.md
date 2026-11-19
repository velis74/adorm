# How-to guides

Recipes for specific tasks:

| Guide | Task |
|---|---|
| [Multiple database users](./multiple-database-users) | one login account, the user's `role` as a connection option of a profile or of the `on_acquire` hook |
| [Schema per tenant](./schema-per-tenant) | the current tenant's schema in the `search_path` connection option, e.g. with django-tenants |
| [Writing a driver](./writing-a-driver) | executing adorm operations on another ORM or database |
| [Generating models](./generating-models) | writing adorm models from Django models with `adorm generate`, and checking them in CI |
