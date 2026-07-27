import asyncio
import re
from pathlib import Path

from backfill.config import Settings
from backfill.models import Task


class WorktreeError(RuntimeError):
    pass


BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,119}$")
DISALLOWED_BRANCH_TERMS = ("codex", "claude", "openai", "chatgpt", "gpt")


def validate_branch_name(branch_name: str) -> None:
    lowered = branch_name.lower()
    if not BRANCH_PATTERN.fullmatch(branch_name):
        raise WorktreeError("Branch name contains unsupported characters")
    if branch_name.endswith((".", "/")) or ".." in branch_name or "//" in branch_name:
        raise WorktreeError("Branch name is not valid")
    if any(term in lowered for term in DISALLOWED_BRANCH_TERMS):
        raise WorktreeError("Branch name contains a prohibited attribution term")


class WorktreeManager:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def prepare(self, task: Task) -> Path:
        repo = Path(task.repo_path).expanduser().resolve()
        if not (repo / ".git").exists():
            raise WorktreeError(f"{repo} is not a git repository")
        validate_branch_name(task.branch_name)

        dirty = await self._git(repo, "status", "--porcelain")
        if dirty.strip():
            raise WorktreeError(
                "Repository working tree is dirty; Backfill will not build on unrelated changes"
            )

        await self._git(repo, "remote", "get-url", task.upstream_remote)
        await self._git(repo, "fetch", task.upstream_remote, task.primary_branch, timeout=120)

        branch_exists = await self._git(
            repo,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{task.branch_name}",
            check=False,
        )
        if branch_exists.returncode == 0:
            raise WorktreeError(f"Branch {task.branch_name} already exists")

        worktree = self.settings.resolved_worktree_dir / task.id
        if worktree.exists():
            raise WorktreeError(f"Worktree path already exists: {worktree}")
        worktree.parent.mkdir(parents=True, exist_ok=True)
        await self._git(
            repo,
            "worktree",
            "add",
            "-b",
            task.branch_name,
            str(worktree),
            f"{task.upstream_remote}/{task.primary_branch}",
            timeout=120,
        )
        return worktree

    async def summary(self, worktree: Path) -> str:
        status = await self._git(worktree, "status", "--short", check=False)
        diff = await self._git(worktree, "diff", "--stat", check=False)
        details = []
        if status.stdout.strip():
            details.append(f"Working tree:\n{status.stdout.strip()}")
        if diff.stdout.strip():
            details.append(f"Diff stat:\n{diff.stdout.strip()}")
        return "\n\n".join(details) or "Agent completed without changing tracked files."

    async def _git(
        self,
        cwd: Path,
        *args: str,
        check: bool = True,
        timeout: int = 30,
    ) -> "_CommandResult":
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(timeout):
                stdout, stderr = await process.communicate()
        except TimeoutError as error:
            process.kill()
            await process.wait()
            raise WorktreeError(f"git {' '.join(args)} timed out") from error
        result = _CommandResult(
            returncode=process.returncode or 0,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise WorktreeError(detail or f"git {' '.join(args)} failed")
        return result


class _CommandResult:
    def __init__(self, returncode: int, stdout: str, stderr: str):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def strip(self) -> str:
        return self.stdout.strip()
