A passing stage sequence needs revision evidence across its handoffs.

The combined flow already ran every role, but its test only checked each role's
status. Capturing final Git refs in the run artifact lets the E2E check prove
that verification, merge, and deployment refer to the same committed code.
The live PR service also rejects an unapproved or mismatched verification before
it exposes PR tools, and its merge uses the verified branch SHA as an exact-head
condition after CI succeeds on that SHA.
