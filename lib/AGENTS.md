# WordPress Coding Standards

All PHP code in this directory MUST adhere to the [WordPress Coding Standards (WPCS)](https://github.com/WordPress/WordPress-Coding-Standards).

- **Formatting:** Real tabs (not spaces) for indentation. Proper spacing inside parentheses, arrays, and control structures (e.g., `if ( condition ) { ... }`)
- **Naming:** Lowercase with underscores for variables, functions, and files (e.g., `$my_variable`, `my_function()`, `my-file.php`)
- **Yoda Conditions:** Always use Yoda conditions for equality checks against constants, `true`, `false`, or integers (e.g., `if ( true === $my_variable )`)
- **Sanitization & Escaping:** All untrusted data must be sanitized before processing and escaped before output
- **SQL Preparation:** All database queries must use `$wpdb->prepare()` to prevent SQL injection
- **Verification:** Run `phpcs` (`./vendor/bin/phpcs`) if available before presenting changes
