# Changelog

## 0.0.5 - 2026-11-19

- Connection option `search_path` (PostgreSQL's `search_path`): a list or tuple of schema names, set like `role` from
  the profile, the session, a `connection_options()` block or an `on_acquire` hook. Recipe: schema per tenant.

## 0.0.4 - 2026-11-18

- `adorm.testing.wait_for_lock()` reports a lock wait of every request a slot runs, not only of operations: a
  `commit` that waits for the lock of a deferred foreign key is reported. `LockWait.request` is the waiting request;
  `LockWait.operation` is `None` for requests that are not an `execute`.
- A slot closed in the loop step in which its connection call ends delivers that call's outcome to the caller and
  stops serving. Before, the caller got `TypeError: invalid exception object` or `Slot.close()` and the event loop's
  shutdown never finished.

## 0.0.3 - 2026-11-17

- Deadlock detection checks each pool of the waiting tasks once per pass instead of once per waiting task: with
  many tasks waiting for a slot, waiting for and releasing a slot no longer costs time in proportion to the number
  of waiting tasks times the pool size.

## 0.0.2 - 2026-11-17

- PyPI project page: installation, an example and links to the documentation; documentation and changelog links
  and the author's e-mail in the package metadata.

## 0.0.1 - 2026-11-17

- Initial release.
