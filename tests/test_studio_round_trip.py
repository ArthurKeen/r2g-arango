"""The Studio hands back every mapping setting it was given.

Opening a project builds the Studio's editor from ``/graph-data``'s config, and Save
rebuilds the mapping from the editor. A field missing from either side is silently dropped
by the next save: an LPG mapping came back as the default layout, without its extra labels,
and the naming convention was never sent at all.
"""

from __future__ import annotations

import re
from pathlib import Path

from r2g.generators.visualizer import MappingVisualizer
from r2g.types import CollectionMapping, EdgeDefinition, MappingConfig, Schema

STATIC = Path(__file__).resolve().parents[1] / "src" / "r2g" / "ui" / "static"


def lpg_mapping() -> MappingConfig:
    return MappingConfig.model_validate({
        "collections": {"books": {"source_table": "books", "target_collection": "books",
                                  "extra_labels": ["Item"]}},
        "graph_layout": "lpg",
        "lpg": {"node_collection": "things"},
        "naming_convention": {"collections": "snake", "properties": "camel", "edges": "snake"},
    })


def test_the_editors_copy_carries_the_mapping_wide_settings_and_extra_labels():
    data = MappingVisualizer(Schema(tables={}), lpg_mapping())._build_config_data()
    assert data["collections"]["books"]["extraLabels"] == ["Item"]
    assert data["graphLayout"] == "lpg"
    assert data["lpg"]["node_collection"] == "things"
    assert data["namingConvention"] == {"collections": "snake", "properties": "camel",
                                        "edges": "snake"}
    # Positive control: a mapping that never set them reports the defaults.
    plain = MappingVisualizer(Schema(tables={}), MappingConfig())._build_config_data()
    assert plain["graphLayout"] == "pg" and plain["namingConvention"] is None


def test_the_page_keeps_them_when_it_opens_a_project_and_sends_them_on_save():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    for needle in ("graphLayout: data.config?.graphLayout || 'pg'",
                   "lpg: data.config?.lpg || null",
                   "namingConvention: data.config?.namingConvention || null",
                   "editState.namingConvention = config.naming_convention",
                   "payload.naming_convention = editState.namingConvention"):
        assert needle in page, needle


def _camel(name: str) -> str:
    return re.sub(r"_(\w)", lambda m: m.group(1).upper(), name)


#: Fields deliberately not round-tripped by the Studio, each with the reason. Empty: every
#: field of the mapping model must survive opening a project and saving it.
NOT_CARRIED: dict[str, str] = {}


def test_every_field_of_the_mapping_model_survives_a_reopen_and_save():
    """The cause behind the lost terms, layout, labels and naming convention: a field the
    model gains but the Studio does not carry both ways is dropped by the next save. This
    fails for the next such field until the Studio carries it (or it is listed above)."""
    config = MappingConfig.model_validate({
        "collections": {"a": {"source_table": "a", "target_collection": "a"}},
        "edges": [{"edge_collection": "e", "from_collection": "a", "to_collection": "a",
                   "from_field": "x", "to_field": "y"}]})
    data = MappingVisualizer(Schema(tables={}), config)._build_config_data()
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    builder = page[page.index("function buildMappingPayload"):]
    builder = builder[:builder.index("\n}\n")]

    def between(start: str, end: str) -> str:
        return builder[builder.index(start):builder.index(end)]

    # Each model's own part of the builder, so a name two models share (``term``) is
    # found where that model is written, not elsewhere.
    sent = {MappingConfig: builder[builder.index("  const payload = {"):],
            CollectionMapping: between("for (const [key, c] of Object.entries(editState.collections",
                                       "collections[key] = entry;"),
            EdgeDefinition: between("const edges = (editState.edges", "    return base;")}
    # An edge's field lists are sent in their singular form when they hold one column.
    sent_as = {"from_fields": ("from_fields", "from_field"), "to_fields": ("to_fields", "to_field")}
    missing = []
    for model, copy in [(MappingConfig, data), (CollectionMapping, data["collections"]["a"]),
                        (EdgeDefinition, data["edges"][0])]:
        for name in model.model_fields:
            where = f"{model.__name__}.{name}"
            if where in NOT_CARRIED:
                continue
            names = sent_as.get(name, (name,))
            if not any(_camel(n) in copy for n in names):
                missing.append(f"{where} is not in /graph-data's config")
            if not any(re.search(rf"\b{n}\b", sent[model]) for n in names):
                missing.append(f"{where} is not sent by buildMappingPayload")
    assert not missing, missing
