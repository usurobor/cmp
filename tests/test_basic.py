from cmp import __version__, say_hello


def test_version_and_function():
    assert isinstance(__version__, str)
    assert "cmp" in say_hello()
