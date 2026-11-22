# slugify spec

`slugify(text)` turns a title into a URL slug:

1. lower-case;
2. every run of characters that are not letters or digits becomes ONE hyphen;
3. no leading or trailing hyphen.

Examples: `"Hello World"` -> `"hello-world"`, `"Hello   World!"` -> `"hello-world"`.
