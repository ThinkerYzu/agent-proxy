"""Publication must reject mismatched, withdrawn and stale distributions."""
import io
from pathlib import Path
import tarfile
import zipfile

import pytest

from agentproxy import codex_cli
from tools.check_release import check_release


@pytest.fixture
def project(tmp_path):
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "agentproxy"\nversion = "0.4.0"\n')
    module = tmp_path / 'src/agentproxy/__init__.py'
    module.parent.mkdir(parents=True)
    module.write_text('__version__ = "0.4.0"\n')
    (tmp_path / 'RELEASE-NOTES.md').write_text('# agent-proxy v0.4.0\n\nReady for publication.\n')
    return tmp_path


def distributions(root, wheel_name='agentproxy', wheel_version='0.4.0', source_name='agentproxy', source_version='0.4.0'):
    dist = root / 'dist'
    dist.mkdir()
    with zipfile.ZipFile(dist / 'agentproxy-0.4.0-py3-none-any.whl', 'w') as wheel:
        wheel.writestr('agentproxy-0.4.0.dist-info/METADATA',
                       f'Metadata-Version: 2.4\nName: {wheel_name}\nVersion: {wheel_version}\n')
    raw = f'Metadata-Version: 2.4\nName: {source_name}\nVersion: {source_version}\n'.encode()
    with tarfile.open(dist / 'agentproxy-0.4.0.tar.gz', 'w:gz') as source:
        entry = tarfile.TarInfo('agentproxy-0.4.0/PKG-INFO')
        entry.size = len(raw)
        source.addfile(entry, io.BytesIO(raw))
    return dist


def test_release_accepts_matching_tag_and_both_distribution_metadata(project):
    assert check_release(project, 'v0.4.0', distributions(project)) == ('agentproxy', '0.4.0')


@pytest.mark.parametrize('tag', ['v0.3.0', '0.4.0', 'master'])
def test_release_rejects_wrong_tag_before_publishing(project, tag):
    with pytest.raises(ValueError, match='must match'):
        check_release(project, tag)


def test_release_rejects_module_version_mismatch(project):
    (project / 'src/agentproxy/__init__.py').write_text('__version__ = "0.3.0"')
    with pytest.raises(ValueError, match='must match'):
        check_release(project, 'v0.4.0')


@pytest.mark.parametrize('heading', ['# agent-proxy v0.4.0 (withdrawn)', '# agent-proxy v0.3.0', ''])
def test_release_rejects_withdrawn_or_wrong_notes(project, heading):
    (project / 'RELEASE-NOTES.md').write_text(heading)
    with pytest.raises(ValueError, match='release-note heading'):
        check_release(project, 'v0.4.0')


@pytest.mark.parametrize('metadata', [
    {'wheel_name': 'different-project'}, {'source_name': 'different-project'},
    {'wheel_version': '0.3.0'}, {'source_version': '0.3.0'},
])
def test_release_rejects_mismatched_artifact_name_or_version(project, metadata):
    with pytest.raises(ValueError, match='metadata must name'):
        check_release(project, 'v0.4.0', distributions(project, **metadata))


@pytest.mark.parametrize('extra', ['old-package-0.2.0.whl', 'README.txt', 'agentproxy-0.3.0.tar.gz'])
def test_release_rejects_stale_or_unexpected_output_files(project, extra):
    dist = distributions(project)
    (dist / extra).write_text('stale output')
    with pytest.raises(ValueError, match='exactly one wheel'):
        check_release(project, 'v0.4.0', dist)


def test_release_rejects_missing_sdist(project):
    dist = distributions(project)
    (dist / 'agentproxy-0.4.0.tar.gz').unlink()
    with pytest.raises(ValueError, match='exactly one wheel'):
        check_release(project, 'v0.4.0', dist)


def test_release_rejects_filename_disagreement_even_when_metadata_matches(project):
    dist = distributions(project)
    wheel = dist / 'agentproxy-0.4.0-py3-none-any.whl'
    wheel.rename(dist / 'different-0.4.0-py3-none-any.whl')
    with pytest.raises(ValueError, match='artifact filename'):
        check_release(project, 'v0.4.0', dist)


def test_codex_reports_the_renamed_distribution_version(monkeypatch):
    looked_up = []
    def version(name):
        looked_up.append(name)
        return '0.4.0'
    monkeypatch.setattr(codex_cli.metadata, 'version', version)
    assert codex_cli._version() == '0.4.0'
    assert looked_up == ['agentproxy']


def test_release_allows_uv_build_ignore_marker_without_publishing_it(project):
    dist = distributions(project)
    (dist / '.gitignore').write_text('*\n')
    assert check_release(project, 'v0.4.0', dist) == ('agentproxy', '0.4.0')
