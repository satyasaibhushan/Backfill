import pytest

from backfill.worktrees import WorktreeError, validate_branch_name


@pytest.mark.parametrize(
    "branch_name",
    ["feature/codex-run", "claude-maintenance", "openai-task", "gpt-cleanup"],
)
def test_attribution_terms_are_rejected(branch_name: str) -> None:
    with pytest.raises(WorktreeError, match="prohibited attribution"):
        validate_branch_name(branch_name)


@pytest.mark.parametrize("branch_name", ["release-checks", "SULF-1234", "fix/api.guard"])
def test_normal_branch_names_are_allowed(branch_name: str) -> None:
    validate_branch_name(branch_name)
