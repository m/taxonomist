# Project Instructions: WordPress Coding Standards

**CRITICAL MANDATE:**
All PHP code written, modified, or reviewed in this project MUST strictly adhere to the [WordPress Coding Standards (WPCS)](https://github.com/WordPress/WordPress-Coding-Standards).

- **Formatting:** Use real tabs (not spaces) for indentation. Ensure proper spacing inside parentheses, arrays, and control structures (e.g., `if ( condition ) { ... }`).
- **Naming Conventions:** Use lowercase letters with words separated by underscores for variable, function, and file names (e.g., `$my_variable`, `my_function()`, `my-file.php`).
- **Yoda Conditions:** Always use Yoda conditions when checking equality against constants, `true`, `false`, or integers (e.g., `if ( true === $my_variable )`).
- **Sanitization & Escaping:** All untrusted data must be sanitized before processing and escaped before output.
- **SQL Preparation:** All database queries must be prepared using `$wpdb->prepare()` to prevent SQL injection.
- **Verification:** Before presenting changes, always run the existing `phpcs` tools (`./vendor/bin/phpcs`) if available, to ensure compliance.
