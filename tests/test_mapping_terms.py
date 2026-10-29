"""P14.8: a mapping element names the ontology term it stands for.

A table names a class, a column or relationship a property, each an IRI from the project
ontology. The terms are optional and kept in the mapping file. Under a control plane a save
that names a new term is checked against the project ontology, read from AOE with the
user's context; without one nothing reads them. Each refusal has a positive control.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from r2g import control_plane
from r2g.catalog import CatalogManager
from r2g.config import ConfigManager
from r2g.types import CollectionMapping, EdgeDefinition, MappingConfig
from r2g.ui.server import create_app

TOKEN = "context-for-equity"
BOOK = "https://ex.org/lending#Book"
AUTHOR = "https://ex.org/lending#Author"
TITLE = "https://ex.org/lending#title"
WROTE = "https://ex.org/lending#wrote"
ONTOLOGY = {"ontology_id": "lending", "version": "working", "problem": None,
            "classes": [{"uri": BOOK, "label": "Book"}, {"uri": AUTHOR, "label": "Author"}],
            "properties": [{"uri": TITLE, "label": "title"}, {"uri": WROTE, "label": "wrote"}]}
STATIC = Path(__file__).resolve().parents[1] / "src" / "r2g" / "ui" / "static"


def mapping(**collection: Any) -> dict[str, Any]:
    books = {"source_table": "books", "target_collection": "books", **collection}
    return {"collections": {"books": books,
                            "authors": {"source_table": "authors", "target_collection": "authors"}},
            "edges": [{"edge_collection": "wrote", "from_collection": "books",
                       "to_collection": "authors", "from_field": "author_id", "to_field": "id"}]}


class TestTheMappingFile:
    def test_a_mapping_without_terms_is_written_as_before(self):
        dumped = MappingConfig.model_validate(mapping()).model_dump()
        assert "term" not in dumped["collections"]["books"]
        assert "field_terms" not in dumped["collections"]["books"]
        assert "term" not in dumped["edges"][0]

    def test_terms_round_trip_through_the_yaml_file(self, tmp_path):
        body = mapping(term=BOOK, field_terms={"title": TITLE})
        body["edges"][0]["term"] = WROTE
        path = tmp_path / "mapping.yaml"
        ConfigManager.save_config(MappingConfig.model_validate(body), path)
        on_disk = yaml.safe_load(path.read_text())
        assert on_disk["collections"]["books"]["term"] == BOOK
        assert on_disk["collections"]["books"]["field_terms"] == {"title": TITLE}
        assert on_disk["edges"][0]["term"] == WROTE
        loaded = ConfigManager.load_config(path)
        assert loaded.collections["books"].term == BOOK and loaded.edges[0].term == WROTE

    def test_a_term_must_be_an_absolute_iri(self):
        with pytest.raises(ValidationError, match="absolute IRI"):
            CollectionMapping(source_table="books", target_collection="books", term="Book")
        with pytest.raises(ValidationError, match="absolute IRI"):
            CollectionMapping(source_table="books", target_collection="books",
                              field_terms={"title": "has title"})
        with pytest.raises(ValidationError, match="absolute IRI"):
            EdgeDefinition(edge_collection="wrote", from_collection="books",
                           to_collection="authors", from_field="author_id", to_field="id",
                           term="wrote by")
        # Positive control: an IRI is kept, trimmed; a blank one is no term at all.
        coll = CollectionMapping(source_table="books", target_collection="books",
                                 term=f" {BOOK} ", field_terms={"title": TITLE, "isbn": " "})
        assert coll.term == BOOK and coll.field_terms == {"title": TITLE}


class TestTheOntologyFromAoe:
    IDENTITY = control_plane.Identity("pj@example.com", ("analyst",), "local", "library",
                                      ("lending", "working"), token=TOKEN)

    @staticmethod
    def answer(status: int, body: dict[str, Any]) -> MagicMock:
        response = MagicMock()
        response.status_code = status
        response.json.return_value = body
        return response

    def test_every_page_of_classes_and_the_properties_are_read(self, monkeypatch):
        monkeypatch.setenv("AOE_URL", "http://aoe:8000/aoe/")
        http = MagicMock()
        http.get.side_effect = [
            self.answer(200, {"data": [{"uri": BOOK, "label": "Book"}], "next_cursor": "c2"}),
            self.answer(200, {"data": [{"uri": AUTHOR, "label": "Author"}, {"label": "no uri"}],
                              "next_cursor": None}),
            self.answer(200, {"data": [{"uri": TITLE, "label": "title"}]}),
        ]
        out = control_plane.ontology_terms(self.IDENTITY, http=http)
        assert out["problem"] is None
        assert [c["uri"] for c in out["classes"]] == [AUTHOR, BOOK]
        assert [p["uri"] for p in out["properties"]] == [TITLE]
        urls = [call.args[0] for call in http.get.call_args_list]
        assert urls == ["http://aoe:8000/aoe/api/v1/ontology/lending/classes"] * 2 + [
            "http://aoe:8000/aoe/api/v1/ontology/lending/properties"]
        assert http.get.call_args_list[1].kwargs["params"]["cursor"] == "c2"
        assert all(call.kwargs["headers"] == {"Authorization": f"Bearer {TOKEN}"}
                   for call in http.get.call_args_list)

    def test_a_refused_property_read_is_reported_with_no_partial_list(self, monkeypatch):
        monkeypatch.setenv("AOE_URL", "http://aoe:8000/aoe")
        http = MagicMock()
        http.get.side_effect = [self.answer(200, {"data": [{"uri": BOOK}]}),
                                self.answer(403, {})]
        out = control_plane.ontology_terms(self.IDENTITY, http=http)
        assert out["classes"] == [] and out["properties"] == []
        assert "403" in out["problem"]


class TestWhichTermsAreChecked:
    def test_only_a_term_that_is_new_or_changed_where_it_is_named(self):
        saved = MappingConfig.model_validate(mapping(term="https://ex.org/gone#Book"))
        new = MappingConfig.model_validate(mapping(term="https://ex.org/gone#Book",
                                                   field_terms={"title": "https://ex.org/x"}))
        # The saved class is no longer in the ontology, but it is not checked again.
        assert control_plane.unknown_terms(saved, new, ONTOLOGY) == [
            "collections/books/title: https://ex.org/x is not a property of lending"]

    def test_a_table_needs_a_class_and_a_column_or_relationship_a_property(self):
        body = mapping(term=TITLE, field_terms={"title": BOOK})
        body["edges"][0]["term"] = AUTHOR
        unknown = control_plane.unknown_terms(None, MappingConfig.model_validate(body), ONTOLOGY)
        assert unknown == [
            f"collections/books: {TITLE} is not a class of lending",
            f"collections/books/title: {BOOK} is not a property of lending",
            f"edges/wrote: {AUTHOR} is not a property of lending",
        ]
        # Positive control: the right kinds are accepted.
        body = mapping(term=BOOK, field_terms={"title": TITLE})
        body["edges"][0]["term"] = WROTE
        assert control_plane.unknown_terms(None, MappingConfig.model_validate(body),
                                           ONTOLOGY) == []


class FakeControlPlane:
    def verify(self, token: str) -> control_plane.Identity:
        if token != TOKEN:
            raise control_plane.ContextRejected("bad signature")
        return control_plane.Identity("pj@example.com", ("analyst",), "local", "library",
                                      ("lending", "working"), token=token)

    def project(self, identity: control_plane.Identity) -> control_plane.Project:
        return control_plane.Project("library", "Library", "library-catalog")


@pytest.fixture
def catalog_dir(tmp_path: Path) -> Path:
    catalog = CatalogManager(tmp_path)
    catalog.add_source("src", "csv", str(tmp_path))
    (tmp_path / "library-catalog.yaml").write_text(yaml.safe_dump(mapping()))
    catalog.create_project("library-catalog", "src", str(tmp_path / "library-catalog.yaml"))
    return tmp_path


@pytest.fixture
def governed(catalog_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONTROL_PLANE_URL", "http://cp.test/api")
    monkeypatch.setenv("R2G_TENANT", "local")
    control_plane.set_instance(FakeControlPlane())
    reads: list[str] = []

    def ontology_terms(identity: control_plane.Identity, **_: Any) -> dict[str, Any]:
        reads.append(identity.token)
        return ontology_terms.answer

    ontology_terms.answer = ONTOLOGY  # type: ignore[attr-defined]
    monkeypatch.setattr(control_plane, "ontology_terms", ontology_terms)
    client = TestClient(create_app(catalog_dir=str(catalog_dir), host="0.0.0.0"))
    yield client, ontology_terms, reads
    control_plane.set_instance(None)


def save(client: TestClient, body: dict[str, Any]):
    return client.put("/api/projects/library-catalog/mapping", json=body,
                      headers={"Authorization": f"Bearer {TOKEN}"})


def saved_books(catalog_dir: Path) -> dict[str, Any]:
    return yaml.safe_load((catalog_dir / "library-catalog.yaml").read_text())["collections"]["books"]


class TestSavingUnderAControlPlane:
    def test_terms_from_the_project_ontology_are_saved(self, governed, catalog_dir):
        client, _, reads = governed
        r = save(client, mapping(term=BOOK, field_terms={"title": TITLE}))
        assert r.status_code == 200, r.text
        assert saved_books(catalog_dir)["term"] == BOOK
        assert reads == [TOKEN]                     # read with the user's own context

    def test_a_term_not_in_the_project_ontology_is_refused_and_nothing_is_written(
            self, governed, catalog_dir):
        client, _, _ = governed
        r = save(client, mapping(term="https://ex.org/other#Invoice"))
        assert r.status_code == 400
        assert "https://ex.org/other#Invoice is not a class of lending" in r.text
        assert "term" not in saved_books(catalog_dir)

    def test_an_unreadable_ontology_refuses_a_new_term_but_not_other_edits(
            self, governed, catalog_dir):
        client, ontology_terms, _ = governed
        ontology_terms.answer = {**ONTOLOGY, "classes": [], "properties": [],
                                 "problem": "cannot reach AOE: timed out"}
        r = save(client, mapping(term=BOOK))
        assert r.status_code == 503 and "cannot reach AOE" in r.text
        assert "term" not in saved_books(catalog_dir)
        # Positive control: a save naming no new term needs no ontology, and goes through.
        r = save(client, mapping(field_mappings={"title": "name"}))
        assert r.status_code == 200, r.text
        assert saved_books(catalog_dir)["field_mappings"] == {"title": "name"}

    def test_a_save_that_names_no_new_term_does_not_read_the_ontology(self, governed):
        client, _, reads = governed
        assert save(client, mapping(term=BOOK)).status_code == 200
        reads.clear()
        assert save(client, mapping(term=BOOK, field_mappings={"title": "name"})).status_code == 200
        assert reads == []


def test_without_a_control_plane_terms_are_kept_and_not_looked_up(catalog_dir, monkeypatch):
    monkeypatch.delenv("CONTROL_PLANE_URL", raising=False)
    monkeypatch.setattr(control_plane, "ontology_terms",
                        MagicMock(side_effect=AssertionError("not read outside a control plane")))
    client = TestClient(create_app(catalog_dir=str(catalog_dir)))
    r = client.put("/api/projects/library-catalog/mapping",
                   json=mapping(term="https://ex.org/anything#Book"))
    assert r.status_code == 200, r.text
    assert saved_books(catalog_dir)["term"] == "https://ex.org/anything#Book"


def test_the_page_carries_terms_both_ways_so_a_save_never_drops_them():
    """The Studio rebuilds the mapping from its own state on save, so a field it does not
    carry is lost. It reads and writes every term in any mode."""
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    for needle in ("term: c.term || null", "fieldTerms: c.field_terms || c.fieldTerms || {}",
                   "term: e.term || null", "if (c.term) entry.term = c.term;",
                   "entry.field_terms = c.fieldTerms", "if (e.term) base.term = e.term;"):
        assert needle in page, needle


def test_the_editors_copy_of_the_mapping_carries_the_terms():
    """Opening a project builds the Studio's editor from ``/graph-data``'s config, and a
    save rebuilds the mapping from the editor: a term left out there is dropped."""
    from r2g.generators.visualizer import MappingVisualizer
    from r2g.types import Schema

    body = mapping(term=BOOK, field_terms={"title": TITLE})
    body["edges"][0]["term"] = WROTE
    data = MappingVisualizer(Schema(tables={}),
                             MappingConfig.model_validate(body))._build_config_data()
    assert data["collections"]["books"]["term"] == BOOK
    assert data["collections"]["books"]["fieldTerms"] == {"title": TITLE}
    assert data["collections"]["authors"]["term"] is None
    assert [e["term"] for e in data["edges"]] == [WROTE]
