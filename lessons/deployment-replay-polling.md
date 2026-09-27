Let scripted replay react to stateful tool results when polling for readiness.

A fixed health-call count stopped after an SSH check returned a transient
`warming` result. Reading each tool result before choosing the next call makes
replay cover both eventual health and script failure without hard-coded counts.
