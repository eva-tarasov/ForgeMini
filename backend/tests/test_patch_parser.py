import pytest
from pydantic import ValidationError

from app.tools.repository.patch import (
    MAX_FILE_LINES,
    MAX_PATCH_BYTES,
    MAX_PATCH_FILES,
    MAX_PATCH_HUNKS,
    PatchTextError,
    apply_file_patch,
    parse_patch,
)
from app.tools.repository.schemas import ApplyPatchInput


def section(path="file.txt", body="@@ -1,1 +1,1 @@\n-before\n+after\n"):
    return f"--- a/{path}\n+++ b/{path}\n{body}"


@pytest.mark.parametrize(
    ("original", "body", "expected"),
    [
        ("before\nrest\n", "@@ -1,1 +1,1 @@\n-before\n+after\n", "after\nrest\n"),
        ("first\nrest\n", "@@ -1,1 +1,2 @@\n first\n+added\n", "first\nadded\nrest\n"),
        ("first\nremove\nrest\n", "@@ -1,2 +1,1 @@\n first\n-remove\n", "first\nrest\n"),
    ],
    ids=["replace", "insert", "remove"],
)
def test_apply_file_patch_replaces_inserts_and_removes_exactly(original, body, expected):
    (file,) = parse_patch(section(body=body)).files
    assert apply_file_patch(original, file) == expected


def test_parser_handles_multiple_hunks_and_files_preserving_lf_text():
    original = "one\nunchanged \t\n三\u2028行\nlast\n"
    first = section("src/текст.txt", "@@ -1,1 +1,2 @@\n-one\n+first\n+extra\n@@ -3,1 +4,1 @@\n-三\u2028行\n+新\u2028行\n")
    parsed = parse_patch(first + section("notes.txt"))
    assert parsed.paths == ("src/текст.txt", "notes.txt")
    assert apply_file_patch(original, parsed.files[0]) == "first\nextra\nunchanged \t\n新\u2028行\nlast\n"
    assert apply_file_patch("before\n", parsed.files[1]) == "after\n"


@pytest.mark.parametrize(
    "text",
    [
        "", "\n", "--- a/file.txt\n", section(body=""), section(body="@@ -1,1 +1,1 @@\n before\n"),
        section(body="@@ -1,2 +1,1 @@\n-before\n+after\n"),
        section(body="@@ -1,1 +1,1 @@\n-before\n+after\n+extra\n"),
        section(body="@@ -1,0 +1,1 @@\n+after\n"),
        section(body="@@ -1,1 +0,0 @@\n-before\n"),
        section(body="@@ -1 +1 @@\n-before\n+after\n"),
        section(body="@@ -1,1 +1,1 @@ heading\n-before\n+after\n"),
        section(body="@@ -1,1 +2,1 @@\n-before\n+after\n"),
        section(body="@@ -1,1 +1,1 @@\n-before\n+after\n@@ -1,1 +1,1 @@\n-before\n+other\n"),
        section(body="@@ -2,1 +2,1 @@\n-before\n+after\n@@ -1,1 +1,1 @@\n-before\n+other\n"),
        section(body="@@ -1,1 +1,1 @@\n-before\n+before\n"),
        section() + section(),
        section().rstrip("\n"), section().replace("\n", "\r\n"), section().replace("+after", "+a\0b"),
        section().replace("+after", "+\ud800"),
        section() + "\\ No newline at end of file\n",
        "diff --git a/file.txt b/file.txt\n" + section(),
        "rename from old\nrename to new\n" + section(),
        "old mode 100644\nnew mode 100755\n" + section(),
        "GIT binary patch\nliteral 0\n",
        section().replace("--- a/file.txt", "--- /dev/null"),
        section().replace("+++ b/file.txt", "+++ /dev/null"),
        section().replace("--- a/file.txt", "--- file.txt"),
        section().replace("+++ b/file.txt", "+++ b/other.txt"),
        section().replace("+++ b/file.txt", "+++ b/file.txt\t2026-11-16"),
    ],
)
def test_parser_rejects_malformed_or_unsupported_syntax(text):
    with pytest.raises(PatchTextError):
        parse_patch(text)


@pytest.mark.parametrize(
    "path",
    ["/absolute", "../outside", "src/../file", "a//file", "a/./file", ".", "a\\file", "C:note",
     "a b", "~home", ".GiT/config", "deep/.ENV.local", ".forge-patch-unrelated", "/".join(["x"] * 17)],
)
def test_parser_rejects_unsafe_patch_targets(path):
    with pytest.raises(PatchTextError, match="^invalid_patch_path$"):
        parse_patch(section(path))


@pytest.mark.parametrize(
    "text",
    [
        section(body="@@ -1,1 +1,1 @@\n-before\n+" + "界" * (MAX_PATCH_BYTES // 3) + "\n"),
        "".join(section(f"file-{index}") for index in range(MAX_PATCH_FILES + 1)),
        section(body="".join(f"@@ -{index},1 +{index},1 @@\n-a\n+b\n" for index in range(1, MAX_PATCH_HUNKS + 2))),
        section(body=f"@@ -{MAX_FILE_LINES},2 +{MAX_FILE_LINES},2 @@\n-before\n+after\n context\n"),
    ],
    ids=["utf8-byte-budget", "file-budget", "hunk-budget", "coordinate-budget"],
)
def test_parser_enforces_patch_work_budgets(text):
    with pytest.raises(PatchTextError, match="^patch_limit_exceeded$"):
        parse_patch(text)


@pytest.mark.parametrize(
    ("text", "body", "code"),
    [
        ("moved\nbefore\n", "@@ -1,1 +1,1 @@\n-before\n+after\n", "patch_conflict"),
        ("before\n", "@@ -2,1 +2,1 @@\n-before\n+after\n", "patch_conflict"),
        ("before\n", "@@ -1,2 +1,1 @@\n-before\n-other\n+after\n", "patch_conflict"),
        (
            "repeat\nrepeat\nrepeat\n",
            "@@ -1,1 +1,2 @@\n repeat\n+repeat\n@@ -2,2 +3,1 @@\n repeat\n-repeat\n",
            "patch_no_change",
        ),
        ("before\r\n", "@@ -1,1 +1,1 @@\n-before\n+after\n", "patch_unsupported_text"),
        ("before", "@@ -1,1 +1,1 @@\n-before\n+after\n", "patch_unsupported_text"),
        ("before\n" * (MAX_FILE_LINES + 1), "@@ -1,1 +1,1 @@\n-before\n+after\n", "patch_limit_exceeded"),
        ("before\n" * MAX_FILE_LINES, "@@ -1,1 +1,2 @@\n before\n+added\n", "patch_limit_exceeded"),
        ("before\n" + "x" * 65535 + "\n", "@@ -1,1 +1,1 @@\n-before\n+after\n", "patch_limit_exceeded"),
        ("before\n" + "x" * 65528 + "\n", "@@ -1,1 +1,1 @@\n-before\n+longest\n", "patch_limit_exceeded"),
    ],
)
def test_apply_file_patch_rejects_stale_input_noops_and_invalid_file_budgets(text, body, code):
    (file,) = parse_patch(section(body=body)).files
    with pytest.raises(PatchTextError, match=f"^{code}$"):
        apply_file_patch(text, file)


def test_patch_input_is_strict_and_uses_the_same_parser():
    text = section()
    assert ApplyPatchInput.model_validate({"patch": text}).patch == text
    for value in (b"not text", "malformed", "", {"path": "file.txt"}):
        with pytest.raises(ValidationError):
            ApplyPatchInput.model_validate({"patch": value})
    with pytest.raises(ValidationError):
        ApplyPatchInput.model_validate({"patch": text, "paths": ["file.txt"]})
