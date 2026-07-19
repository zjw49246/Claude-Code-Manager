"""Regression tests for low-latency skill discovery."""

from unittest.mock import patch

from backend.services import skill_loader


def test_discover_skills_reuses_parsed_metadata_within_ttl(tmp_path):
    skill_dir = tmp_path / "repo" / "skills" / "fast-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: fast-skill\ndescription: cached\n---\nBody\n",
        encoding="utf-8",
    )
    skill_loader._discovery_cache.clear()

    with patch.object(
        skill_loader,
        "parse_skill",
        wraps=skill_loader.parse_skill,
    ) as parse:
        first = skill_loader.discover_skills(ccm_repo_dir=tmp_path / "repo")
        second = skill_loader.discover_skills(ccm_repo_dir=tmp_path / "repo")

    assert list(first) == ["fast-skill"]
    assert list(second) == ["fast-skill"]
    assert parse.call_count == 1


def test_discover_skills_cache_still_applies_per_call_filters(tmp_path):
    skill_dir = tmp_path / "repo" / "skills" / "role-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: role-skill\n"
        "description: role-specific\n"
        "ccm:\n"
        "  roles: [admin]\n"
        "---\n"
        "Body\n",
        encoding="utf-8",
    )
    skill_loader._discovery_cache.clear()

    assert "role-skill" in skill_loader.discover_skills(
        ccm_repo_dir=tmp_path / "repo", role="admin"
    )
    assert skill_loader.discover_skills(
        ccm_repo_dir=tmp_path / "repo", role="viewer"
    ) == {}
