# Contributing to Taxonomist

## Prerequisites

- PHP 7.4+
- Python 3.8+
- [Composer](https://getcomposer.org/)

## Setup

```bash
composer install
```

This installs [WordPress Coding Standards (WPCS)](https://github.com/WordPress/WordPress-Coding-Standards) for PHP linting.

## Running Tests

### Python tests

The helper functions and WordPress.com adapter are covered by a unittest suite in `tests/`:

```bash
python3 -m unittest discover tests
```

Bug fixes must include a regression test in `tests/`.

## Linting

### PHP (WPCS)

Lint all PHP files in `lib/`:

```bash
./vendor/bin/phpcs
```

Auto-fix what can be fixed automatically:

```bash
./vendor/bin/phpcbf
```

The ruleset is defined in `.phpcs.xml.dist`.

## Before You Commit

Run both checks:

```bash
./vendor/bin/phpcs
python3 -m unittest discover tests
```

Both must pass before submitting a pull request.
