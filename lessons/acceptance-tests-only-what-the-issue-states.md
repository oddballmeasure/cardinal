An acceptance test may check only what its issue states; an unstated detail turns the suite into a coin flip.

The note-form issue asks for "a note form with required title and comma-separated tags fields",
but its acceptance test clicked a button named exactly "Add note". Cardinal's first full live run
happened to use that label and passed; the second built a working form with a different label,
was approved by its verifier, merged, and failed the hidden test. The diary-form issue, by
contrast, names its "Create diary" action, so its test is fair.

The note-form test now submits through the form's submit control. It was checked both ways
before rerunning: 3/3 fail at baseline 7a2e92a, 3/3 pass on Cardinal's PR #17 commit. When a live
case fails only on a selector or wording, check the issue text before blaming the product.

A second oracle defect surfaced the same way: `test_filter_and_csv_combine` created notes tagged
`Csv-Only-Tag`/`csv-only-tag` but expected CSV rows tagged `Blue`/`BLUE`, left over from an
earlier rename. Cardinal's correct output failed it. Fixed and checked both ways (5/5 on PR #22,
4/5 fail at baseline; the fifth checks unchanged default behavior).

A third, caught before it fired: both UI tests looked fields up page-wide with
`get_by_label("Title")`, a case-insensitive substring match. Once the diary form joins the note
form on one page, every Title lookup is ambiguous and both tests fail, so the suite could never
pass cumulatively. Each test now scopes lookups to its own form (the one with Tags, the one with
Date), since the issues speak of "a note form" and a diary form. Re-verified: 3/3 on PR #17,
3/3 fail at baseline.
