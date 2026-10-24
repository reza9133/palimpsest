import hashlib

import pytest


@pytest.fixture
def actors(direct_accounts):
    """Ten framework accounts plus deterministic extras for volume tests."""
    extra = [hashlib.sha256(f"palimpsest-extra-{i}".encode()).digest()[:20] for i in range(12)]
    return list(direct_accounts) + extra


@pytest.fixture(autouse=True)
def _latest_llm_mock_wins(direct_vm):
    """Make a later ``mock_llm`` for the same pattern replace the earlier one.

    genlayer-test appends mocks and answers with the first match, so a test that serves one
    verdict in its setup and then re-mocks the same pattern with another would keep
    receiving the first. Replacing in place keeps hit-tracking indices stable.
    """
    original = direct_vm.mock_llm

    def mock_llm(prompt_pattern, response):
        for index, (pattern, _old) in enumerate(direct_vm._llm_mocks):
            if pattern.pattern == prompt_pattern:
                direct_vm._llm_mocks[index] = (pattern, response)
                return
        original(prompt_pattern, response)

    direct_vm.mock_llm = mock_llm
    yield
