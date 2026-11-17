# Changelog

## 0.0.3 - 2026-11-17

- Deadlock detection checks each pool of the waiting tasks once per pass instead of once per waiting task: with
  many tasks waiting for a slot, waiting for and releasing a slot no longer costs time in proportion to the number
  of waiting tasks times the pool size.

## 0.0.2 - 2026-11-17

- PyPI project page: installation, an example and links to the documentation; documentation and changelog links
  and the author's e-mail in the package metadata.

## 0.0.1 - 2026-11-17

- Initial release.
