from app.services.chunk_reconstruction import reconstruct_chunks


def test_reconstruction_removes_repeated_class_context():
    chunks = [
        ({"chunk_index": 0, "symbol_name": "Service.one"}, "class Service:\n    def one(self):\n        return 1\n"),
        ({"chunk_index": 1, "symbol_name": "Service.two"}, "class Service:\n    def two(self):\n        return 2\n"),
    ]

    content = reconstruct_chunks(chunks)

    assert content.count("class Service:") == 1
    assert "def one" in content
    assert "def two" in content