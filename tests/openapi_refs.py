"""Resolve openapi.json's local $refs.

The document defines every block the paid routes share (the provenance and
attribution schemas, the language input, the credential headers) once under
components and points at it. A test that compares a route's schema with the
catalog's compares what a reader resolves, which is what this returns.
"""


_WHOLE = object()


def resolved(doc, node=_WHOLE):
    # A sentinel, not None: null is a value schemas carry ("examples": [null]).
    if node is _WHOLE:
        node = doc
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            target = doc
            for part in ref[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            return resolved(doc, target)
        return {key: resolved(doc, value) for key, value in node.items()}
    if isinstance(node, list):
        return [resolved(doc, value) for value in node]
    return node
