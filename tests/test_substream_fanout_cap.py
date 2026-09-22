from katzenqt import network


def test_a_cap_on_unresolved_substreams_exists() -> None:
    assert isinstance(network._MAX_OPEN_SUBSTREAMS_PER_PEER, int)
    assert 0 < network._MAX_OPEN_SUBSTREAMS_PER_PEER <= 32


def test_the_indirection_path_consults_the_cap() -> None:
    import ast
    import inspect
    import io
    import textwrap

    path = inspect.getsourcefile(network)
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
        and n.name == "drain_mixwal_read_single"
    )
    names = {
        n.id for n in ast.walk(fn)
        if isinstance(n, ast.Name)
    }
    assert "_MAX_OPEN_SUBSTREAMS_PER_PEER" in names, (
        "a peer can announce unbounded concurrent substreams, each buying a "
        "full miss budget of mixnet reads before it fails"
    )
