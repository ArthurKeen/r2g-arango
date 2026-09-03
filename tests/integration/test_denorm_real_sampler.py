"""Denormalization detection against a REAL sampler (PLAN §5.2).

The seam this covers has never been exercised. The measuring half (the value
probes) lives in `relational-schema-analyzer`; the deciding half (the detectors)
lives in `r2g.denorm`. r2g's 25 unit tests inject a fake sampler, and RSA's
sampler tests have no engine to drive — so the join between them, the part most
likely to break, was covered by neither. RSA reported that two CSV probes raised
`TypeError` against real rows and had been shipping that way; because
`denorm.py` degrades a failing probe to `None`, the symptom was not a crash but
the silent disappearance of every sampling-based finding, leaving only the
structural detectors asserting.

These tests use the real `CsvValueSampler` over real CSV files, so a probe that
raises makes them fail rather than quietly narrowing the result. No database is
required, which is why this can be an always-on guard rather than a gated one.

Fixture shape is the one PLAN-denormalization-analysis.md §5.2 specifies: an
embedded lookup, `zip -> city,state`.
"""

from __future__ import annotations

import pytest

from r2g.denorm import AnalyzeOptions, analyze_denormalization
from r2g.fk_inference import CsvValueSampler
from r2g.types import Column, Schema, Table


def _write(tmp_path, name: str, header: str, rows: list[str]):
    path = tmp_path / f"{name}.csv"
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def addresses(tmp_path):
    """`zip` functionally determines `city`/`state` — a textbook embedded lookup.

    Every row for a given zip repeats the same city and state, so the FD holds
    perfectly and `group_single_valued` should report 1.0.
    """
    rows = []
    for i in range(40):
        z, city, state = [
            ("02138", "Cambridge", "MA"),
            ("10001", "New York", "NY"),
            ("94105", "San Francisco", "CA"),
            ("60601", "Chicago", "IL"),
        ][i % 4]
        rows.append(f"{i},{z},{city},{state}")
    _write(tmp_path, "addresses", "id,zip,city,state", rows)
    return tmp_path


def _schema() -> Schema:
    return Schema(tables={"addresses": Table(
        name="addresses",
        columns=[
            Column(name="id", data_type="integer", is_primary_key=True),
            Column(name="zip", data_type="text"),
            Column(name="city", data_type="text"),
            Column(name="state", data_type="text"),
        ],
        primary_key=["id"],
    )})


class TestProbesAgainstRealRows:
    """The probes themselves — these are what raised TypeError before RSA 0.7.2."""

    def test_distinct_ratio_returns_a_real_number(self, addresses):
        sampler = CsvValueSampler(str(addresses))
        # 4 distinct zips over 40 rows.
        assert sampler.distinct_ratio("addresses", "zip") == pytest.approx(0.1, abs=0.02)

    def test_group_single_valued_detects_a_perfect_dependency(self, addresses):
        sampler = CsvValueSampler(str(addresses))
        assert sampler.group_single_valued("addresses", ["zip"], "city") == pytest.approx(1.0)

    def test_group_single_valued_detects_a_broken_dependency(self, addresses):
        """The negative case, so a probe that always returns 1.0 cannot pass."""
        sampler = CsvValueSampler(str(addresses))
        # `id` is unique, so it determines city perfectly; city does NOT determine id.
        assert sampler.group_single_valued("addresses", ["city"], "id") < 1.0

    def test_delimiter_rate_returns_a_real_number(self, addresses):
        sampler = CsvValueSampler(str(addresses))
        rate = sampler.delimiter_rate("addresses", "city", ",")
        assert rate is not None and rate == pytest.approx(0.0)

    def test_no_probe_raises(self, addresses):
        """The regression this file exists for: all three probes callable on real
        rows. Before RSA 0.7.2 two of them raised TypeError here."""
        sampler = CsvValueSampler(str(addresses))
        assert sampler.distinct_ratio("addresses", "zip") is not None
        assert sampler.group_single_valued("addresses", ["zip"], "state") is not None
        assert sampler.delimiter_rate("addresses", "state", ",") is not None


class TestEngineDrivenByRealProbes:
    """The join: r2g's detectors consuming RSA's real measurements."""

    def _findings(self, addresses, **kw):
        return analyze_denormalization(
            _schema(),
            options=AnalyzeOptions(sample=True, min_confidence=0.0, **kw),
            sampler=CsvValueSampler(str(addresses)),
        )

    def test_embedded_lookup_is_found(self, addresses):
        """zip -> city,state, detected end to end through the real sampler."""
        kinds = {(f.kind, tuple(f.columns)) for f in self._findings(addresses)}
        embedded = {cols for kind, cols in kinds if kind == "embedded_lookup"}
        assert embedded, f"no embedded_lookup found; got {kinds}"
        assert any("zip" in cols for cols in embedded)

    def test_sampling_findings_disappear_without_a_sampler(self, addresses):
        """Pins the failure mode a broken probe produces: not a crash, but the
        silent loss of every sampling-based finding. If the probes regress, the
        two result sets converge and this fails."""
        with_sampler = self._findings(addresses)
        without = analyze_denormalization(
            _schema(), options=AnalyzeOptions(sample=True, min_confidence=0.0), sampler=None)
        sampled_kinds = {f.kind for f in with_sampler} - {f.kind for f in without}
        assert "embedded_lookup" in sampled_kinds

    def test_findings_carry_an_action_and_real_evidence(self, addresses):
        """Every finding must say what to do and cite the measurement behind it —
        the evidence is what proves a real probe ran, not a degraded default."""
        findings = self._findings(addresses)
        assert findings
        for f in findings:
            assert f.recommended_action, f"no action on {f.kind}"
            assert f.evidence, f"no evidence on {f.kind}"

    def test_evidence_quotes_the_measured_ratio(self, addresses):
        """The FD is perfect in the fixture, so the probe must report 100%."""
        embedded = [f for f in self._findings(addresses) if f.kind == "embedded_lookup"]
        assert embedded
        assert any("100%" in e for f in embedded for e in f.evidence), \
            [e for f in embedded for e in f.evidence]

    def test_determinant_and_dependents_are_identified(self, addresses):
        embedded = [f for f in self._findings(addresses) if f.kind == "embedded_lookup"]
        assert embedded
        f = embedded[0]
        assert f.determinant == ["zip"]
        assert set(f.dependents) == {"city", "state"}
