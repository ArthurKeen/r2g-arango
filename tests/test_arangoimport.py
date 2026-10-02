from __future__ import annotations

import shlex
import stat

import pytest

from r2g.config import ConfigManager
from r2g.generators.arangoimport import ArangoImportGenerator
from r2g.types import CollectionMapping, EdgeDefinition, MappingConfig


@pytest.fixture
def simple_config():
    return MappingConfig(
        collections={
            "users": CollectionMapping(source_table="users", target_collection="users"),
            "orders": CollectionMapping(source_table="orders", target_collection="orders"),
        },
        edges=[
            EdgeDefinition(
                edge_collection="orders_to_users",
                from_collection="orders",
                to_collection="users",
                from_field="user_id",
                to_field="id",
            ),
        ],
    )


@pytest.fixture
def generator(simple_config):
    return ArangoImportGenerator(simple_config)


class TestGenerateDocumentCommands:
    def test_produces_commands_for_each_document_collection(self, generator):
        commands = generator.generate_document_commands()
        assert len(commands) == 2

    def test_commands_contain_collection_names(self, generator):
        commands = generator.generate_document_commands()
        joined = "\n".join(commands)
        assert "users" in joined
        assert "orders" in joined

    def test_commands_use_document_type(self, generator):
        commands = generator.generate_document_commands()
        for cmd in commands:
            assert "--create-collection-type document" in cmd

    def test_commands_include_arangoimport(self, generator):
        commands = generator.generate_document_commands()
        for cmd in commands:
            assert cmd.startswith("arangoimport")


class TestGenerateEdgeCommands:
    def test_produces_commands_for_each_edge(self, generator):
        commands = generator.generate_edge_commands()
        assert len(commands) == 1

    def test_commands_contain_edge_type(self, generator):
        commands = generator.generate_edge_commands()
        for cmd in commands:
            assert "--create-collection-type edge" in cmd

    def test_commands_contain_edge_collection_name(self, generator):
        commands = generator.generate_edge_commands()
        assert "orders_to_users" in commands[0]


class TestGenerateScript:
    def test_shebang_present(self, generator, tmp_path):
        path = str(tmp_path / "import.sh")
        content = generator.generate_script(path)
        assert content.startswith("#!/usr/bin/env bash")

    def test_set_pipefail_present(self, generator, tmp_path):
        path = str(tmp_path / "import.sh")
        content = generator.generate_script(path)
        assert "set -euo pipefail" in content

    def test_file_is_executable(self, generator, tmp_path):
        path = tmp_path / "import.sh"
        generator.generate_script(str(path))
        mode = path.stat().st_mode
        assert mode & stat.S_IXUSR
        assert mode & stat.S_IXGRP
        assert mode & stat.S_IXOTH

    def test_script_contains_document_and_edge_sections(self, generator, tmp_path):
        path = str(tmp_path / "import.sh")
        content = generator.generate_script(path)
        assert "users" in content
        assert "orders" in content
        assert "orders_to_users" in content

    def test_script_written_to_disk(self, generator, tmp_path):
        path = tmp_path / "import.sh"
        generator.generate_script(str(path))
        assert path.exists()
        disk_content = path.read_text(encoding="utf-8")
        assert disk_content.startswith("#!/usr/bin/env bash")


class TestStructuredPreview:
    def test_plan_preserves_mapping_identity_and_cli_commands(
        self, generator, tmp_path
    ):
        plan = generator.build_plan(graph_name="demo")
        written = generator.generate_script(str(tmp_path / "import.sh"))

        assert [item.mapping_id for item in plan.documents] == [
            "users",
            "orders",
        ]
        assert plan.edges[0].mapping_id == "orders_to_users"
        for item in (*plan.documents, *plan.edges):
            assert item.command in written
        assert written.index("Importing document") < written.index(
            "Importing edge"
        )

    def test_secret_safe_plan_never_emits_credentials(self, simple_config):
        generator = ArangoImportGenerator(
            simple_config,
            endpoint="http://admin:sentinel-target-secret@localhost:8529",
            password="sentinel-password",
        )

        plan = generator.build_plan()

        assert "sentinel-password" not in plan.script
        assert "sentinel-target-secret" not in plan.script
        assert '"$ARANGO_PASSWORD"' in plan.script

    def test_plan_uses_fixed_jsonl_artifact_root(self, generator):
        plan = generator.build_plan()
        assert plan.data_dir == "./output"
        assert "./output/users.jsonl" in plan.documents[0].command

    def test_custom_artifact_root_is_shell_quoted(self, simple_config):
        artifact_dir = "./demo artifacts;touch should-not-run"
        plan = ArangoImportGenerator(
            simple_config,
            data_dir=artifact_dir,
        ).build_plan()
        command = shlex.split(plan.documents[0].command)

        assert command[command.index("--file") + 1] == (
            f"{artifact_dir}/users.jsonl"
        )

    def test_graph_artifact_is_runnable_secret_safe_shell(
        self, simple_config
    ):
        plan = ArangoImportGenerator(
            simple_config,
            endpoint="http://admin:target-secret@localhost:8529",
            password="password-secret",
        ).build_plan(graph_name="demo")

        assert plan.graph_script.startswith("#!/usr/bin/env bash")
        assert "arangosh" in plan.graph_script
        assert '"$ARANGO_PASSWORD"' in plan.graph_script
        assert "password-secret" not in plan.graph_script
        assert "target-secret" not in plan.graph_script
        assert "# === Named graph ===" in plan.script

    def test_build_plan_builds_each_spec_group_once(
        self, generator, monkeypatch
    ):
        document_calls = 0
        edge_calls = 0
        original_documents = generator.build_document_specs
        original_edges = generator.build_edge_specs

        def documents(*args, **kwargs):
            nonlocal document_calls
            document_calls += 1
            return original_documents(*args, **kwargs)

        def edges(*args, **kwargs):
            nonlocal edge_calls
            edge_calls += 1
            return original_edges(*args, **kwargs)

        monkeypatch.setattr(generator, "build_document_specs", documents)
        monkeypatch.setattr(generator, "build_edge_specs", edges)

        generator.build_plan()

        assert document_calls == 1
        assert edge_calls == 1

    def test_lpg_preview_is_rejected(self, simple_config):
        config = simple_config.model_copy(update={"graph_layout": "lpg"})
        with pytest.raises(ValueError, match="does not support LPG"):
            ArangoImportGenerator(config).build_plan()

    def test_renamed_collections_are_used_in_graph_artifact(self):
        config = MappingConfig(
            collections={
                "orders": CollectionMapping(
                    source_table="orders",
                    target_collection="Purchase",
                ),
                "users": CollectionMapping(
                    source_table="users",
                    target_collection="Customer",
                ),
            },
            edges=[
                EdgeDefinition(
                    edge_collection="PLACED_BY",
                    from_collection="orders",
                    to_collection="users",
                    from_field="user_id",
                    to_field="id",
                )
            ],
        )
        graph = ArangoImportGenerator(config).build_plan().graph_script
        assert '"Purchase"' in graph
        assert '"Customer"' in graph


class TestGenerateCreateGraphAql:
    def test_contains_graph_name(self, generator):
        aql = generator.generate_create_graph_aql("my_graph")
        assert "my_graph" in aql

    def test_contains_edge_definitions(self, generator):
        aql = generator.generate_create_graph_aql()
        assert "orders_to_users" in aql
        assert "orders" in aql
        assert "users" in aql

    def test_contains_relation_call(self, generator):
        aql = generator.generate_create_graph_aql()
        assert "graph._relation(" in aql

    def test_contains_create_call(self, generator):
        aql = generator.generate_create_graph_aql()
        assert "graph._create(" in aql

    def test_default_graph_name(self, generator):
        aql = generator.generate_create_graph_aql()
        assert "r2g_graph" in aql


class TestInvalidOnDuplicate:
    def test_raises_value_error(self, simple_config):
        with pytest.raises(ValueError, match="on_duplicate"):
            ArangoImportGenerator(simple_config, on_duplicate="bad_value")

    @pytest.mark.parametrize("valid", ["error", "update", "replace", "ignore"])
    def test_valid_values_accepted(self, simple_config, valid):
        gen = ArangoImportGenerator(simple_config, on_duplicate=valid)
        assert gen.on_duplicate == valid


class TestFromSampleSchema:
    def test_generate_from_schema(self, sample_schema, tmp_path):
        config = ConfigManager.generate_default_config(sample_schema)
        gen = ArangoImportGenerator(config)

        doc_cmds = gen.generate_document_commands()
        edge_cmds = gen.generate_edge_commands()
        assert len(doc_cmds) == 2
        assert len(edge_cmds) == 1

        path = str(tmp_path / "import.sh")
        content = gen.generate_script(path)
        assert "orders_to_users" in content
