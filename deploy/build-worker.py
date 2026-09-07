"""Build the worker artifact included in a website deployment."""
import shutil
import subprocess
from pathlib import Path

repo = Path(__file__).resolve().parents[1]
artifact = repo / "src/backfill/worker.whl"
artifact.unlink(missing_ok=True)
subprocess.run(["uv", "build", "--wheel", "--out-dir", str(repo / "dist")], cwd=repo, check=True)
shutil.copyfile(repo / "dist/backfill-0.3.0-py3-none-any.whl", artifact)
print("Worker package ready")
