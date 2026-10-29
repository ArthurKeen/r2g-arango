"""The Studio hands back every mapping setting it was given.

Opening a project builds the Studio's editor from ``/graph-data``'s config, and Save
rebuilds the mapping from the editor. A field missing from either side is silently dropped
by the next save: an LPG mapping came back as the default layout, without its extra labels,
and the naming convention was never sent at all.
"""

from __future__ import annotations

from pathlib import Path

from r2g.generators.visualizer import MappingVisualizer
from r2g.types import MappingConfig, Schema

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
