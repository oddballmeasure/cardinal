A recursion limit derived from turns silently shrinks when middleware adds graph nodes (ported from Fleet).

Fleet computed `recursion_limit = 2 * turns + 3`; adding middleware made each turn cost four
supersteps, so 60 configured turns bought 31. The `GraphRecursionError` handler also reset state
before computing cost, reporting `$0.0000` for real spend, and the error was misread as a
capability failure that escalated to a pricier model.

In Cardinal: keep recursion limits generous, record usage from the messages that exist when the
error fires, and classify `GraphRecursionError` as turn exhaustion, not a model verdict.
