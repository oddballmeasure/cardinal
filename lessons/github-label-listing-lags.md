A label edit is not immediately visible to `gh issue list --label`, so the live suite parks every ready issue and waits for the listing to agree.

The first live smoke run of the product selected only the easy case, removed `cardinal:ready`
from it, re-added it, and started `cardinal daemon --once`. The daemon's listing did not yet show
issue #1 as ready, and five other suite issues still carried the label from before, so it claimed
#2 (medium) instead. It was stopped before pushing; its label had to be restored by hand because
cleanup only covered the selected case.

The live suite now removes `ready` from every open issue at the start, records each issue's
original labels, restores all of them in cleanup, and after labelling a case polls the listing
until it shows exactly that issue. The product's daemon does not work around the lag: a label it
cannot see yet is picked up on its next poll.
