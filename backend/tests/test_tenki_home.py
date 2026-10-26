"""Exercise the actual guest script against temporary homes and data disks."""
import os
import shutil
from pathlib import Path

import pytest

from app.computer.tenki_home import home_script


@pytest.fixture
def home_disk(tmp_path, monkeypatch):
    home, disk = tmp_path / "home", tmp_path / "disk"
    home.mkdir()
    disk.mkdir()
    # Windows test hosts may lack the developer-mode symlink privilege.
    probe = tmp_path / "link"
    try:
        probe.symlink_to(disk, target_is_directory=True)
    except OSError:
        pytest.skip("Host cannot create symlinks; the live Linux canary covers this script")
    probe.unlink()
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path) == disk)
    return home, disk


def prepare(home, disk, vm="vm-one", owner="owned"):
    exec(home_script(owner=owner, vm_id=vm, home=str(home), root=str(disk)), {})


def test_home_and_gui_files_survive_replacement_without_overwriting_saved_settings(home_disk):
    home, disk = home_disk
    (home / ".config").mkdir()
    (home / ".config" / "settings").write_text("user settings")
    prepare(home, disk)
    for folder in ("Desktop", "Documents", "Downloads"):
        (home / folder / "proof.txt").write_text(folder)
    # Files added directly in HOME are collected by the next attach/pause.
    (home / "notes.txt").write_text("user notes")
    prepare(home, disk)
    assert (disk / ".home" / "notes.txt").read_text() == "user notes"
    for entry in home.iterdir():
        entry.unlink()
    (home / ".config").mkdir()
    (home / ".config" / "settings").write_text("replacement image defaults")
    (home / ".config" / "new-default").write_text("additional default")
    prepare(home, disk, vm="vm-two")
    for folder in ("Desktop", "Documents", "Downloads"):
        assert (home / folder / "proof.txt").read_text() == folder
    assert (home / "notes.txt").read_text() == "user notes"
    assert (home / ".config" / "settings").read_text() == "user settings"
    assert (home / ".config" / "new-default").read_text() == "additional default"
    backups = list((disk / ".band-home-conflicts").glob("*/.config/settings"))
    assert len(backups) == 1 and backups[0].read_text() == "replacement image defaults"
    prepare(home, disk, vm="vm-two")  # Reattaching does not replace or duplicate data.
    assert len(list((disk / ".band-home-conflicts").glob("*/.config/settings"))) == 1


def test_failed_copy_keeps_original_and_never_publishes_partial_durable_file(home_disk, monkeypatch):
    home, disk = home_disk
    source = home / "valuable.txt"
    source.write_text("complete original")
    def fail_copy(source, staged):
        Path(staged).write_text("partial")
        raise OSError("simulated interrupted transfer")
    with monkeypatch.context() as patch:
        patch.setattr(shutil, "copy2", fail_copy)
        with pytest.raises(OSError):
            prepare(home, disk)
    assert source.read_text() == "complete original"
    assert not (disk / ".home" / "valuable.txt").exists()
    prepare(home, disk)
    assert source.read_text() == "complete original" and source.is_symlink()


def test_home_migration_refuses_another_owners_disk(home_disk):
    home, disk = home_disk
    (disk / ".band-owner").write_text("another owner")
    (home / "proof.txt").write_text("untouched")
    with pytest.raises(AssertionError, match="owner mismatch"):
        prepare(home, disk)
    assert not (home / "proof.txt").is_symlink()
    assert (home / "proof.txt").read_text() == "untouched"


def test_provider_readiness_files_stay_on_root_disk_for_resume(home_disk):
    home, disk = home_disk
    probe = home / ".tenki-readiness"
    probe.write_text("provider managed")
    (home / ".tenki").mkdir()
    (home / ".tenki" / "runtime").write_text("provider runtime")
    prepare(home, disk)
    prepare(home, disk)
    assert not probe.is_symlink() and probe.read_text() == "provider managed"
    assert not (home / ".tenki").is_symlink()
    assert not (disk / ".home" / ".tenki-readiness").exists()
