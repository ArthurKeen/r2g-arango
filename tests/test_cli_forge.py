"""CLI tests for ``r2g forge generate`` (Federation Forge walking skeleton).

Pure in-memory + tmp_path — no live database. The end-to-end roundtrip lives
in ``tests/integration/test_forge_roundtrip.py``.
"""

from __future__ import annotations

import json
import sys

import pytest
from typer.testing import CliRunner

from r2g.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_structlog(monkeypatch):
    import structlog

    def _stderr_setup(level: str = "INFO", json_output: bool = False) -> None:
        structlog.configure(
            wrapper_class=structlog.make_filtering_bound_logger(0),
            logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
            cache_logger_on_first_use=False,
        )

    monkeypatch.setattr("r2g.log.setup_logging", _stderr_setup)
    monkeypatch.setattr("r2g.main.setup_logging", _stderr_setup)
    _stderr_setup()
    yield
    structlog.reset_defaults()


@pytest.fixture
def ontology_file(tmp_path):
    path = tmp_path / "ontology.json"
    path.write_text(
        json.dumps(
            {
                "entities": [
                    {
                        "name": "Account",
                        "properties": [{"name": "accountName", "type": "string"}],
                    },
                    {
                        "name": "Contact",
                        "properties": [{"name": "isPrimary", "type": "boolean"}],
                    },
                ],
                "relationships": [
                    {
                        "type": "contactsToAccounts",
                        "fromEntity": "Contact",
                        "toEntity": "Account",
                    }
                ],
            }
        )
    )
    return path


def test_generate_writes_artifacts(ontology_file, tmp_path):
    out_dir = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "forge",
            "generate",
            "--ontology",
            str(ontology_file),
            "--seed",
            "421",
            "--out-dir",
            str(out_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Forged" in result.output
    ddl = (out_dir / "forge.sql").read_text()
    assert "CREATE TABLE accounts" in ddl
    assert "FOREIGN KEY (account_id) REFERENCES accounts (id)" in ddl
    load_sql = (out_dir / "forge.load.sql").read_text()
    assert load_sql.count("INSERT INTO contacts") == 10
    rows = json.loads((out_dir / "forge.rows.json").read_text())
    assert set(rows) == {"accounts", "contacts"}


def test_generate_is_deterministic_across_invocations(ontology_file, tmp_path):
    outputs = []
    for run in ("a", "b"):
        out_dir = tmp_path / run
        result = runner.invoke(
            app,
            ["forge", "generate", "--ontology", str(ontology_file), "--seed", "7", "--out-dir", str(out_dir)],
        )
        assert result.exit_code == 0, result.output
        outputs.append(
            tuple((out_dir / name).read_text() for name in ("forge.sql", "forge.load.sql", "forge.rows.json"))
        )
    assert outputs[0] == outputs[1]


def test_refused_ontology_exits_2_with_reason(tmp_path):
    path = tmp_path / "colliding.json"
    path.write_text(
        json.dumps(
            {
                "entities": [
                    {"name": "Account", "properties": [{"name": "label", "type": "string"}]},
                    {"name": "Contact", "properties": [{"name": "label", "type": "string"}]},
                ]
            }
        )
    )
    result = runner.invoke(app, ["forge", "generate", "--ontology", str(path)])
    assert result.exit_code == 2
    assert "Forge refused" in result.output
    assert "collision-free" in result.output


def test_unsupported_dialect_exits_2(ontology_file):
    result = runner.invoke(
        app,
        ["forge", "generate", "--ontology", str(ontology_file), "--dialect", "clickhouse"],
    )
    assert result.exit_code == 2
    assert "dialect" in result.output


def test_bad_rows_per_entity_exits_2(ontology_file):
    result = runner.invoke(
        app,
        ["forge", "generate", "--ontology", str(ontology_file), "--rows-per-entity", "0"],
    )
    assert result.exit_code == 2
    assert "rows-per-entity" in result.output


def test_missing_ontology_file_exits_1(tmp_path):
    result = runner.invoke(
        app,
        ["forge", "generate", "--ontology", str(tmp_path / "absent.json")],
    )
    assert result.exit_code == 1
    assert "Failed to generate" in result.output
