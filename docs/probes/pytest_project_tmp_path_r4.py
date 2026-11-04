from pathlib import Path
import uuid
import pytest

ROOT = Path(__file__).resolve().parents[2]

@pytest.fixture
def tmp_path(request):
    target = ROOT / 'docs/probes/pytest-isolated-tmp-r4' / (uuid.uuid4().hex + '-' + request.node.name.replace('[', '_').replace(']', '_'))
    target.mkdir(parents=True, exist_ok=False)
    return target
