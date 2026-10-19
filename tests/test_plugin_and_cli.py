from __future__ import annotations

import pytest

from leak_hunter.cli import main

pytest_plugins = ["pytester"]


def test_leak_check_fixture_fails_a_leaking_test(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        class Session:
            pass

        sessions = []

        def test_leaks(leak_check):
            leak_check(lambda: sessions.append(Session()), iterations=5)

        def test_clean(leak_check):
            leak_check(lambda: Session(), iterations=5)
        """
    )

    result = pytester.runpytest()

    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(
        ["*LeakError*", "*test_leak_check_fixture_fails_a_leaking_test.Session  +1 per call*"]
    )


def test_cli_check_exits_1_on_a_leak(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", "app:handle_request_leaky", "--include", "app.", "--iterations", "5"]) == 1
    assert "app.RunTrace  +1 per call" in capsys.readouterr().out


def test_cli_check_exits_0_when_fixed(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", "app:handle_request_fixed", "--include", "app.", "--iterations", "5"]) == 0
    assert capsys.readouterr().out.startswith("No object type grew")


def test_cli_why_finds_the_native_holder(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["why", "app:handle_many_leaky", "app.RunTrace"]) == 0

    out = capsys.readouterr().out
    assert "outside Python: nothing the garbage collector can see refers to this _contextvars.Context" in out
    assert "app.RunTrace  <- the object being explained" in out


def test_cli_rejects_a_bad_target() -> None:
    with pytest.raises(SystemExit, match="expected module:function"):
        main(["check", "app"])
