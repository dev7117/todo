def test_deliberately_fails():
    # Throwaway: proves a failing test turns the PR check red. Never merge.
    assert 1 == 2
