from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from database import crud


@pytest.mark.parametrize(
    ("release", "code"),
    [
        (None, "RELEASE_UNRESOLVED"),
        (SimpleNamespace(status="UNQUALIFIED"), "RELEASE_NOT_QUALIFIED"),
        (SimpleNamespace(status="RETIRED"), "RELEASE_WITHDRAWN"),
        (SimpleNamespace(status="BLOCKED"), "RELEASE_WITHDRAWN"),
    ],
)
def test_profile_release_validation_rejects_unavailable_releases(release, code):
    session = MagicMock()
    session.get.return_value = release

    with pytest.raises(crud.ProfileReleaseError) as error:
        crud.validate_profile_release(session, "release-1")

    assert error.value.code == code
    session.add.assert_not_called()
    session.commit.assert_not_called()


def test_profile_release_validation_accepts_qualified_release():
    session = MagicMock()
    session.get.return_value = SimpleNamespace(status="QUALIFIED")

    crud.validate_profile_release(session, "release-1")

    session.add.assert_not_called()
    session.commit.assert_not_called()


def test_profile_release_validation_allows_nullable_release_for_crud_compatibility():
    session = MagicMock()

    crud.validate_profile_release(session, None)

    session.get.assert_not_called()
    session.commit.assert_not_called()