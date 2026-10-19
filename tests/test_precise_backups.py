from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tools import capture_source_provenance as provenance
from tools import create_change_backup as changes
from tools import release_bundle as release
from tools.create_completion_backup import create_backup
from tests.test_completion_backup import make_database


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


@pytest.fixture
def repository(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "fixture@example.test")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "core.autocrlf", "false")
    (root / ".gitignore").write_text("out/\ndata/\n")
    (root / "app.py").write_text("before\n")
    (root / "removed.py").write_text("remove me\n")
    (root / "unchanged.bin").write_bytes(b"unchanged" * 20)
    (root / "archive").mkdir()
    (root / "archive/old.exe").write_bytes(b"retired executable")
    git(root, "add", ".")
    git(root, "commit", "-qm", "fixture baseline")
    monkeypatch.setattr(provenance, "collect_versions", lambda root: {"fixture": True})
    return root


def capture(root, mode="git-delta"):
    output = root / "out/provenance"
    output.mkdir(parents=True, exist_ok=True)
    manifest = provenance.capture(root, patch_output=output / "tracked-changes.patch",
                                  archive_output=output / "source-snapshot.zip", archive_mode=mode)
    provenance.write_manifest(output / "source-provenance.json", manifest)
    return manifest, output


def test_change_backup_preserves_first_contents_and_absent_files(repository):
    root = repository
    output = root / "out/latest.zip"
    original = (root / "app.py").read_bytes()
    changes.create_change_backup(root, output, ["app.py", "new.py"])
    (root / "app.py").write_text("after\n")
    (root / "new.py").write_text("added later\n")
    (root / "old.exe").write_bytes(b"previous executable")
    changes.create_change_backup(root, output, ["app.py", "new.py", "old.exe"])
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {changes.MANIFEST, "RESTORE.txt", "files/app.py", "files/old.exe"}
        assert archive.read("files/app.py") == original
        manifest = json.loads(archive.read(changes.MANIFEST))
        assert manifest["files"]["new.py"] == {"present": False, "size": 0}
        assert manifest["base_commit"] == git(root, "rev-parse", "HEAD").decode().strip()
    before = output.read_bytes()
    changes.create_change_backup(root, output, ["app.py"])
    assert output.read_bytes() == before


@pytest.mark.parametrize("path", ["archive", "../outside.txt", "/absolute", "C:\\outside.txt", ".git/config", "data/cuoti.db", "data/cuoti.db-wal"])
def test_change_backup_refuses_broad_or_unsafe_inputs(repository, path):
    output = repository / "out/latest.zip"
    with pytest.raises(ValueError):
        changes.create_change_backup(repository, output, [path])
    assert not output.exists()


def test_change_backup_keeps_old_archive_on_failed_update(repository, monkeypatch):
    output = repository / "out/latest.zip"
    changes.create_change_backup(repository, output, ["app.py"])
    before = output.read_bytes()
    def fail_replace(*args):
        raise OSError("fixture replacement failure")
    monkeypatch.setattr(changes.os, "replace", fail_replace)
    with pytest.raises(OSError, match="fixture"):
        changes.create_change_backup(repository, output, ["removed.py"])
    assert output.read_bytes() == before
    assert sorted(p.name for p in output.parent.iterdir()) == ["latest.zip"]


def test_git_delta_reconstructs_dirty_tracked_and_untracked_sources(repository, tmp_path):
    root = repository
    (root / "app.py").write_text("after\n")
    (root / "removed.py").unlink()
    (root / "binary.bin").write_bytes(b"\0\1\2new")
    git(root, "add", "binary.bin")
    (root / "new untracked.py").write_text("untracked\n")
    (root / "archive/old.exe").write_bytes(b"changed history must not be copied")
    (root / "Claude outputs").mkdir()
    (root / "Claude outputs/draft.txt").write_text("unrelated output")
    manifest, output = capture(root)
    provenance.validate_source_reference(root, manifest, output / "source-snapshot.zip")
    with zipfile.ZipFile(output / "source-snapshot.zip") as archive:
        assert archive.namelist() == ["new untracked.py"]
        restored = tmp_path / "restored"
        git(root, "clone", "--quiet", "--no-hardlinks", str(root), str(restored))
        git(restored, "checkout", "--quiet", manifest["git"]["head"])
        git(restored, "apply", "--binary", str(output / "tracked-changes.patch"))
        archive.extractall(restored)
    assert not (restored / "removed.py").exists()
    for entry in manifest["source_tree"]["files"]:
        assert (restored / entry["path"]).read_bytes() == (root / entry["path"]).read_bytes()
    assert "archive/old.exe" in manifest["policy_excluded_paths"]
    assert "Claude outputs/draft.txt" in manifest["policy_excluded_paths"]
    assert b"archive/old.exe" not in (output / "tracked-changes.patch").read_bytes()


def test_clean_delta_does_not_copy_committed_sources(repository):
    manifest, output = capture(repository)
    with zipfile.ZipFile(output / "source-snapshot.zip") as archive:
        assert archive.namelist() == []
    assert (output / "tracked-changes.patch").read_bytes() == b""
    assert manifest["source_tree"]["file_count"] == 4


def test_full_source_export_and_old_provenance_remain_supported(repository):
    manifest, output = capture(repository, "full")
    with zipfile.ZipFile(output / "source-snapshot.zip") as archive:
        assert "app.py" in archive.namelist()
        assert not any(path.startswith("archive/") for path in archive.namelist())
    manifest["schema_version"] = 1
    manifest["source_tree"]["archive"].pop("mode")
    provenance.write_manifest(output / "source-provenance.json", manifest)
    release._validate_provenance_contract(output / "source-provenance.json",
                                         output / "source-snapshot.zip", output / "tracked-changes.patch", root=repository)


def test_delta_fails_when_git_base_or_untracked_payload_is_missing(repository):
    (repository / "untracked.py").write_text("needed\n")
    manifest, output = capture(repository)
    with zipfile.ZipFile(output / "source-snapshot.zip", "w"):
        pass
    with pytest.raises(ValueError, match="exactly"):
        provenance.validate_source_reference(repository, manifest, output / "source-snapshot.zip")
    manifest["git"]["head"] = "0" * 40
    with pytest.raises(RuntimeError):
        provenance.validate_source_reference(repository, manifest, output / "source-snapshot.zip")


@pytest.fixture
def release_inputs(repository, monkeypatch):
    root = repository
    _, output = capture(root)
    build_id = "20260927123456"
    previous_version = "20260927031749"
    candidate = root / "out/candidate.exe"
    formal = root / "formal.exe"
    candidate.write_bytes(b"candidate")
    formal.write_bytes(b"previous")
    monkeypatch.setattr(release, "read_product_version", lambda path: build_id if path.read_bytes() == b"candidate" else previous_version)
    acceptance = root / "out/acceptance.json"
    payload = {"status": "passed", "candidate": {"sha256": release.file_sha256(candidate), "product_version": build_id},
               "source_provenance": {"sha256": release.file_sha256(output / "source-provenance.json")},
               "required_gates": sorted(release.REQUIRED_ACCEPTANCE_GATES),
               "gates": {name: True for name in release.REQUIRED_ACCEPTANCE_GATES},
               "executable_only_rollback": {"no_persistent_data_changes": True, "previous_executable_compatible": True}}
    acceptance.write_text(json.dumps(payload))
    return dict(root=root, candidate=candidate, provenance=output / "source-provenance.json", acceptance=acceptance,
                rollback_snapshot=None, formal_executable=formal,
                previous_executable=root / f"out/{build_id}/previous.exe", output=root / f"out/{build_id}/bundle.json",
                receipt_path=root / f"out/{build_id}/receipt.json", build_id=build_id, product_version=build_id,
                previous_product_version=previous_version, schema_before=65, schema_after=65,
                previous_max_schema=65, candidate_min_schema=65, candidate_max_schema=65,
                rollback_mode="executable-only")


def test_executable_only_bundle_verifies_and_publishes_without_database_copy(release_inputs, monkeypatch):
    args = release_inputs
    monkeypatch.setattr(release, "_snapshot_metadata", lambda path: pytest.fail("must not open a data snapshot"))
    bundle = release.prepare_release_bundle(**args)
    assert "snapshot_path" not in bundle["rollback"]
    verified = release.verify_release_bundle(args["root"], args["output"])
    assert verified["snapshot"] is None
    release.publish_release_bundle(args["root"], args["output"], expected_bundle_sha256=verified["bundle_sha256"],
                                   expected_candidate=args["candidate"], expected_provenance=args["provenance"],
                                   expected_product_version=args["product_version"])
    assert args["formal_executable"].read_bytes() == b"candidate"
    assert args["previous_executable"].read_bytes() == b"previous"
    assert not list(args["root"].rglob("*.db"))


@pytest.mark.parametrize("change", ["schema", "missing-proof", "data-change"])
def test_executable_only_bundle_rejects_migration_or_missing_proof(release_inputs, change):
    args = release_inputs
    if change == "schema":
        args.update(schema_after=66, candidate_max_schema=66)
    else:
        payload = json.loads(args["acceptance"].read_text())
        if change == "missing-proof":
            payload.pop("executable_only_rollback")
        else:
            payload["executable_only_rollback"]["no_persistent_data_changes"] = False
        args["acceptance"].write_text(json.dumps(payload))
    with pytest.raises(release.ReleaseBundleError, match="仅 EXE"):
        release.prepare_release_bundle(**args)
    assert not args["output"].exists()


def test_full_snapshot_bundle_and_legacy_mode_remain_supported(release_inputs):
    args = release_inputs
    database = args["root"] / "out/fixture.db"
    make_database(database)
    args["rollback_snapshot"] = create_backup(args["root"], label="small", snapshot_kind="private-no-secrets",
                                              config={"DATABASE": str(database)}, layout="legacy")
    assert not (args["rollback_snapshot"] / "archive").exists()
    args.update(rollback_mode="full-snapshot", schema_before=6, schema_after=6,
                previous_max_schema=6, candidate_min_schema=6, candidate_max_schema=6)
    bundle = release.prepare_release_bundle(**args)
    assert release.verify_release_bundle(args["root"], args["output"])["snapshot"] == args["rollback_snapshot"]
    bundle["rollback"].pop("mode")
    args["output"].write_text(json.dumps(bundle))
    assert release.verify_release_bundle(args["root"], args["output"])["snapshot"] == args["rollback_snapshot"]


def test_full_backup_cli_requires_explicit_kind_before_creating_output(tmp_path):
    script = Path(__file__).resolve().parents[1] / "tools/create_completion_backup.py"
    result = subprocess.run([sys.executable, "-B", str(script), "--root", str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 2
    assert "--kind" in result.stderr
    assert list(tmp_path.iterdir()) == []
