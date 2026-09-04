"""Phase-9 classification gate on the VALUE-SAMPLING paths.

r2g answers "may this column be read?" one way at load time — excluded at or
above the threshold unless `--allow-sensitive`. The two value-sampling paths did
not answer it at all: `denorm` had a `no_sample_columns` hook populated only from
a hand-typed flag, and FK value-overlap sampling had no gate whatsoever. Both now
follow the loader's rule.

These paths emit statistics rather than values, so nothing leaves the process —
but reading a regulated column is a policy decision, and the previous docstring
claimed a protection that did not exist.
"""

from __future__ import annotations

from r2g.classification import sensitive_columns
from r2g.fk_inference import GatedSampler, gated_sampler
from r2g.types import Classification, Column, Schema, Table


def _schema() -> Schema:
    return Schema(tables={
        "users": Table(name="users", columns=[
            Column(name="id", data_type="integer", is_primary_key=True),
            Column(name="email", data_type="text",
                   classification=Classification(tags=["PII.Sensitive"])),
            Column(name="city", data_type="text"),
        ], primary_key=["id"]),
        "orders": Table(name="orders", columns=[
            Column(name="id", data_type="integer", is_primary_key=True),
            Column(name="user_id", data_type="integer"),
        ], primary_key=["id"]),
    })


class _RecordingSampler:
    """Records every probe so a test can assert a column was never touched."""

    def __init__(self):
        self.probed: list[tuple] = []

    def __call__(self, lt, lc, ft, fc):
        self.probed.append(("overlap", lt, lc, ft, fc))
        return 0.9

    def distinct_ratio(self, table, column):
        self.probed.append(("distinct", table, column))
        return 0.5

    def group_single_valued(self, table, det, dep):
        self.probed.append(("fd", table, tuple(det), dep))
        return 1.0

    def delimiter_rate(self, table, column, delimiter):
        self.probed.append(("delim", table, column))
        return 0.0

    def sample_values(self, table, column, limit=5):
        self.probed.append(("values", table, column))
        return ["x"]


class TestSensitiveColumns:
    def test_identifies_classified_columns_in_both_forms(self):
        cols = sensitive_columns(_schema())
        assert "users.email" in cols and "email" in cols

    def test_unclassified_columns_are_not_listed(self):
        cols = sensitive_columns(_schema())
        assert "city" not in cols and "users.city" not in cols

    def test_threshold_is_honoured(self):
        """`email` is PII -> restricted; `notes` is Tier2 -> internal. Raising the
        threshold from internal to restricted should stop excluding `notes` while
        still excluding `email`."""
        sch = _schema()
        sch.tables["users"].columns.append(
            Column(name="notes", data_type="text",
                   classification=Classification(tags=["Tier.Tier2"])))
        at_internal = sensitive_columns(sch, threshold="internal")
        at_restricted = sensitive_columns(sch, threshold="restricted")
        assert "notes" in at_internal and "notes" not in at_restricted
        assert "email" in at_internal and "email" in at_restricted


class TestGatedSampler:
    def _gated(self):
        inner = _RecordingSampler()
        return inner, GatedSampler(inner, sensitive_columns(_schema()))

    def test_classified_column_is_never_probed(self):
        inner, g = self._gated()
        assert g.distinct_ratio("users", "email") is None
        assert g.sample_values("users", "email") == []
        assert g.delimiter_rate("users", "email", ",") is None
        assert inner.probed == [], f"sampler was called: {inner.probed}"

    def test_unclassified_column_passes_through(self):
        inner, g = self._gated()
        assert g.distinct_ratio("users", "city") == 0.5
        assert inner.probed == [("distinct", "users", "city")]

    def test_overlap_blocked_if_EITHER_side_is_classified(self):
        """The statistic is computed from both columns, so one classified side
        is enough to make the probe disclosive."""
        inner, g = self._gated()
        assert g("orders", "user_id", "users", "email") is None
        assert g("users", "email", "orders", "user_id") is None
        assert inner.probed == []

    def test_overlap_allowed_when_neither_side_is_classified(self):
        inner, g = self._gated()
        assert g("orders", "user_id", "users", "id") == 0.9
        assert len(inner.probed) == 1

    def test_functional_dependency_blocked_via_determinant_or_dependent(self):
        inner, g = self._gated()
        assert g.group_single_valued("users", ["email"], "city") is None
        assert g.group_single_valued("users", ["city"], "email") is None
        assert inner.probed == []


class TestGateWiring:
    def test_allow_sensitive_returns_the_sampler_unwrapped(self):
        inner = _RecordingSampler()
        assert gated_sampler(inner, _schema(), allow_sensitive=True) is inner

    def test_no_classified_columns_means_no_wrapper_overhead(self):
        plain = Schema(tables={"t": Table(name="t", columns=[
            Column(name="id", data_type="integer", is_primary_key=True)], primary_key=["id"])})
        inner = _RecordingSampler()
        assert gated_sampler(inner, plain) is inner

    def test_gate_applied_by_default(self):
        inner = _RecordingSampler()
        assert isinstance(gated_sampler(inner, _schema()), GatedSampler)

    def test_none_sampler_stays_none(self):
        assert gated_sampler(None, _schema()) is None

    def test_unknown_attributes_pass_through(self):
        """A wrapper that hid non-probe attributes would break `close()`."""
        inner = _RecordingSampler()
        inner.close = lambda: "closed"
        assert GatedSampler(inner, frozenset({"x"})).close() == "closed"
