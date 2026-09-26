from tsuduri_mcp.server import hello


def test_hello_includes_name():
    assert "つづりちゃん" in hello("つづりちゃん")
