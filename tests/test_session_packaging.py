"""The session harness's inputs, stated as an interface (#68).

``SessionPaths`` makes the harness's fixture roots parameters instead of
file-position arithmetic: the installed wheel fails with a named error —
not a ``FileNotFoundError`` from a ``parents[3]`` walk — and any caller
can point the faces at their own checkout. The packaging smoke installs
the built wheel into a bare directory (a pure-Python wheel extracted is
byte-for-byte what pip lays into site-packages: no deps, no scripts to
rewrite) and imports it from there, so the assertion describes the
published shape, not the developer checkout.
"""

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from net2board.harness.paths import SessionFixturesMissing, SessionPaths
from net2board.harness.session import run_session

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestSessionPaths:
    def test_repo_default_resolves_the_committed_fixtures(self):
        paths = SessionPaths.repo_default()
        assert paths.ecc83_netlist.name == "ecc83-pp.net"
        assert paths.ecc83_netlist.exists()
        assert paths.tiny_netlist.exists()
        assert paths.ecc83_footprints.is_dir()
        assert paths.tiny_footprints.is_dir()

    def test_a_missing_fixture_root_is_a_named_failure(self, tmp_path):
        with pytest.raises(SessionFixturesMissing, match="ecc83-pp\\.net"):
            SessionPaths(ecc83_dir=tmp_path, tiny_dir=Path("kicad_demo/tiny_tapeout"))

    def test_a_missing_tiny_root_is_a_named_failure_too(self, tmp_path):
        paths = SessionPaths.repo_default()
        with pytest.raises(SessionFixturesMissing, match="tinytapeout\\.net"):
            SessionPaths(ecc83_dir=paths.ecc83_dir, tiny_dir=tmp_path)

    def test_an_absent_jar_is_not_a_failure(self, tmp_path):
        """The jar feeds the provenance block's sha256 line only — absent
        is a reading there, so it must not fail construction."""
        paths = SessionPaths.repo_default()
        assert (
            SessionPaths(
                ecc83_dir=paths.ecc83_dir,
                tiny_dir=paths.tiny_dir,
                freerouting_jar=tmp_path / "no-such.jar",
            ).freerouting_jar
            == tmp_path / "no-such.jar"
        )


class TestDefaultFactoryFailureMode:
    def test_run_session_names_the_missing_fixtures(self, monkeypatch, tmp_path):
        """From an installed wheel the repo-relative roots do not exist:
        the default factory fails with the named error, never a bare
        FileNotFoundError from the loader's open()."""
        import net2board.harness.paths as paths_module

        monkeypatch.setattr(paths_module, "REPO_ROOT", tmp_path)
        with pytest.raises(SessionFixturesMissing, match="kicad_demo"):
            run_session()


class TestInstalledWheel:
    """The published shape: build the wheel, lay it where pip would,
    import from there, and read the failure mode."""

    @pytest.fixture(scope="module")
    def site_dir(self, tmp_path_factory) -> Path:
        repo = REPO_ROOT
        dist = tmp_path_factory.mktemp("dist")
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(dist)],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        (wheel,) = dist.glob("*.whl")
        site = tmp_path_factory.mktemp("site")
        # a pure-Python wheel extracted is exactly pip's purelib layout
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(site)
        return site

    def _run_installed(self, site_dir: Path, tmp_path: Path, code: str):
        """Import from the installed-like layout: PYTHONPATH replaces —
        never appends — and the bare cwd gives ``-c`` nothing to find."""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(site_dir)
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_run_session_from_the_installed_wheel_names_the_error(
        self, site_dir, tmp_path
    ):
        code = (
            "import net2board.harness.paths\n"
            "import net2board.harness.session as session\n"
            "try:\n"
            "    session.run_session()\n"
            "except net2board.harness.paths.SessionFixturesMissing as exc:\n"
            "    print('NAMED-ERROR:', exc)\n"
            "else:\n"
            "    raise SystemExit('run_session() did not raise the named error')\n"
        )
        result = self._run_installed(site_dir, tmp_path, code)
        assert result.returncode == 0, result.stderr
        assert "NAMED-ERROR:" in result.stdout
        # the named mode states the missing roots and the injection answer
        assert "kicad_demo" in result.stdout
        assert "SessionPaths" in result.stdout

    def test_the_installed_wheel_imports_clean(self, site_dir, tmp_path):
        result = self._run_installed(
            site_dir,
            tmp_path,
            (
                "import net2board, net2board.harness.session as s, "
                "net2board.examples.session as cli; "
                "print(s.SessionPaths.__name__, cli.main.__module__)"
            ),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "SessionPaths net2board.examples.session"
