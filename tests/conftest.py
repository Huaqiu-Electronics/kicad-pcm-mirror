import pytest

from helpers import UpstreamServer


@pytest.fixture
def upstream():
    server = UpstreamServer()
    yield server
    server.close()
