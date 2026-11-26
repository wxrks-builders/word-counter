import os

import pytest

# The app reads these at import and refuses to start without them. Tests never
# talk to Google, so a fresh clone runs `pytest` without a .env; a real .env's
# values are left alone.
for _name, _value in (("SESSION_SECRET", "test-session-secret"),
                      ("GOOGLE_CLIENT_ID", "test-client-id"),
                      ("GOOGLE_CLIENT_SECRET", "test-client-secret")):
    os.environ.setdefault(_name, _value)


@pytest.fixture(autouse=True)
def _repetition_tallies_in_tmp(monkeypatch, tmp_path):
    """Crawls save their repetition tally next to the database; in tests that
    is a temporary folder, never the working copy's data/."""
    from app import repetition

    monkeypatch.setattr(repetition, "TALLY_DIR", tmp_path / "repetition")


@pytest.fixture(autouse=True)
def _translation_screenshots_in_tmp(monkeypatch, tmp_path):
    """The same for the translation check's screenshots, which a check also
    deletes when it prunes an old one."""
    from app import translation_shots

    monkeypatch.setattr(translation_shots, "SHOTS_DIR", tmp_path / "translation-shots")


@pytest.fixture(autouse=True)
def _consolidation_in_tmp(monkeypatch, tmp_path):
    """Sentence fingerprints and a project's consolidation CSVs go to a
    temporary folder too, and a consolidation runs inside the request that asks
    for it, so a test sees its result straight away."""
    from app import consolidation_job, segment_store

    monkeypatch.setattr(segment_store, "SEGMENT_DIR", tmp_path / "segments")
    monkeypatch.setattr(consolidation_job, "CONSOLIDATION_DIR", tmp_path / "consolidations")
    monkeypatch.setattr(consolidation_job, "INLINE", True)
    monkeypatch.setattr(consolidation_job, "_stopping", False)
    monkeypatch.setattr(consolidation_job, "_tasks", {})
