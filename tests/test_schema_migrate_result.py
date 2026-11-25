"""A schema migration must leave each written file loadable (#1852)."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pyrite import migrations
from pyrite.cli import app
from pyrite.config import KBConfig, PyriteConfig, Settings
from pyrite.services.acting import local_process
from pyrite.services.schema_service import SchemaService


@pytest.fixture(params=["task", "backlog_item"])
def migration_case(request, tmp_path: Path, monkeypatch):
    entry_type = request.param
    kb = tmp_path / "kb"
    entries = kb / "entries"
    entries.mkdir(parents=True)
    (kb / "kb.yaml").write_text(
        f"name: demo\ntypes:\n  {entry_type}:\n    version: 2\n  note:\n    version: 2\n"
    )
    initial = {
        "bad": entry_type,
        "good": entry_type,
        "stamp": "note",
    }
    for entry_id, kind in initial.items():
        (entries / f"{entry_id}.md").write_text(
            f"---\nid: {entry_id}\ntype: {kind}\ntitle: Test entry\n---\n\nBody\n"
        )

    registry = migrations.MigrationRegistry()
    monkeypatch.setattr(migrations, "_registry", registry)
    monkeypatch.setattr(migrations, "_plugins_loaded", True)

    @registry.register(entry_type, 0, 1)
    def first(data):
        data["title"] = "Migrated title"
        return data

    if entry_type == "task":
        field, value, message = "metadata", ["bad"], "is not a mapping"
    else:
        field, value, message = "rank", "not a rank", "invalid literal for int()"

    @registry.register(entry_type, 1, 2)
    def second(data):
        if data["id"] == "bad":
            data[field] = value
        return data

    config = PyriteConfig(
        knowledge_bases=[KBConfig(name="demo", path=kb)],
        settings=Settings(index_path=tmp_path / "index.db", auto_embed=False),
    )
    return config, entries, message


@pytest.mark.parametrize("dry_run", [False, True])
def test_migration_refuses_a_result_that_does_not_load(migration_case, dry_run):
    config, entries, message = migration_case
    before = {p.name: p.read_bytes() for p in entries.iterdir()}

    with local_process():
        result = SchemaService(config).migrate_entries("demo", dry_run=dry_run)

    assert result["checked"] == result["migrated"] == 2
    (error,) = result["errors"]
    assert error["path"] == "entries/bad.md"
    assert "result would not load" in error["error"]
    assert message in error["error"]
    assert (entries / "bad.md").read_bytes() == before["bad.md"]
    if dry_run:
        assert {p.name: p.read_bytes() for p in entries.iterdir()} == before
    else:
        good = (entries / "good.md").read_bytes()
        assert b"title: Migrated title" in good
        assert b"_schema_version: 2" in good
        assert b"_schema_version: 2" in (entries / "stamp.md").read_bytes()


def test_migration_cli_reports_the_refused_file(migration_case, monkeypatch, caplog):
    config, entries, message = migration_case
    monkeypatch.setattr("pyrite.cli.load_config", lambda: config)
    monkeypatch.setattr("pyrite.cli.context.load_config", lambda: config)
    before = (entries / "bad.md").read_bytes()

    with caplog.at_level("WARNING"):
        result = CliRunner().invoke(app, ["schema", "migrate", "--kb", "demo"])

    assert result.exit_code == 3, result.output
    assert "Migrated: 2" in result.output
    assert "Errors: 1" in result.output
    assert any(
        "Migration error for entries/bad.md" in x.message and message in x.message
        for x in caplog.records
    )
    assert (entries / "bad.md").read_bytes() == before
