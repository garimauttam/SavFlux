"""Reconstruct source text from ordered vector-store chunks."""

from collections.abc import Iterable


def reconstruct_chunks(chunks: Iterable[tuple[dict, str]]) -> str:
    """Join chunks while removing repeated AST context and character overlap."""
    ordered = sorted(chunks, key=lambda item: item[0].get("chunk_index", 0))
    result = ""
    previous_class = ""

    for metadata, content in ordered:
        symbol = metadata.get("symbol_name", "")
        class_name = symbol.split(".", 1)[0] if "." in symbol else ""
        if class_name and class_name == previous_class:
            lines = content.splitlines(keepends=True)
            if lines and lines[0].lstrip().startswith("class "):
                content = "".join(lines[1:])

        overlap = min(len(result), len(content), 200)
        while overlap >= 20 and not result.endswith(content[:overlap]):
            overlap -= 1
        if overlap >= 20:
            content = content[overlap:]

        result += content
        previous_class = class_name

    return result