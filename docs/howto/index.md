# How-to guides

How-to guides are recipes for a specific task. Each guide gets its page as the package provides the features
it uses:

| Guide | Task |
|---|---|
| Multiple database users | one login account per connection profile, `SET ROLE` in the `on_acquire` hook and `RESET ROLE` in `on_release` |
| [Writing a driver](./writing-a-driver) | executing adorm operations on another ORM or database |
| [Generating models](./generating-models) | writing adorm models from Django models with `adorm generate`, and checking them in CI |
