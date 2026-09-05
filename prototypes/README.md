# prototypes/

Self-contained explorations that are **not part of the r2g package**.

Nothing here is imported by `src/r2g/`, shipped in the wheel, or covered by the
test suite's guarantees. Each directory stands alone and may depend on services
(a live ArangoDB) that the main suite does not require. Treat the code as
evidence for a design decision rather than as an interface.
