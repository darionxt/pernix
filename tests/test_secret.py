from pernix.settings import Secret


def test_secret_is_redacted_in_repr_str_and_formatting() -> None:
    token = Secret("s3cr3t")

    assert repr(token) == "Secret('**********')"
    assert str(token) == "**********"
    assert f"token={token}" == "token=**********"
    assert "s3cr3t" not in repr([token])


def test_secret_reveals_and_compares_by_value() -> None:
    assert Secret("a").reveal() == "a"
    assert Secret("a") == Secret("a")
    assert Secret("a") != Secret("b")
    assert len({Secret("a"), Secret("a")}) == 1
