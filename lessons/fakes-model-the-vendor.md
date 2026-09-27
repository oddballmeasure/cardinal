A fake built from our own code certifies calls the real service rejects (ported from Fleet).

Fleet's fake GitHub repository implemented `get_issue_comment`, a method PyGithub never had;
tests passed while every production status edit raised and a fallback posted 36 comments.

In Cardinal: the offline fake `gh` must be written from `gh`'s documented flags and JSON fields,
and every command it accepts must also run in the live suite. When a fake-only test fails in a
surprising way, suspect the fake first.
