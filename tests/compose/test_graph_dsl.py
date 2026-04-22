from __future__ import annotations

import pytest

from reelforge_core.compose.graph import FilterGraph, FilterNode, _ffescape
from reelforge_core.errors import GraphError


def test_serialize_with_args() -> None:
    node = FilterNode(
        filter_name="scale",
        inputs=["[0:v]"],
        outputs=["[v]"],
        args={"w": 1920, "h": 1080},
    )
    assert node.serialize() == "[0:v]scale=w=1920:h=1080[v]"


def test_serialize_without_args() -> None:
    node = FilterNode(filter_name="null", inputs=["[a]"], outputs=["[b]"])
    assert node.serialize() == "[a]null[b]"


def test_graph_rejects_duplicate_output_labels() -> None:
    g = FilterGraph()
    g.add(FilterNode("null", inputs=["[0:v]"], outputs=["[x]"]))
    with pytest.raises(GraphError):
        g.add(FilterNode("null", inputs=["[1:v]"], outputs=["[x]"]))


def test_escape_commas_in_expression() -> None:
    # zoompan's z expression contains commas; the DSL escapes them.
    node = FilterNode(
        filter_name="zoompan",
        inputs=["[v]"],
        outputs=["[vz]"],
        args={"z": "min(zoom+0.001,1.1)", "d": 1, "s": "1080x1920"},
    )
    serialized = node.serialize()
    # The escape lib doubles backslashes *then* escapes commas, so we expect \, in output.
    assert r"min(zoom+0.001\,1.1)" in serialized
    assert "d=1" in serialized
    assert "s=1080x1920" in serialized


def test_ffescape_handles_colons_brackets_quotes() -> None:
    assert _ffescape("a:b") == r"a\:b"
    assert _ffescape("a,b") == r"a\,b"
    assert _ffescape("a'b") == r"a\'b"
    assert _ffescape("a[b]c") == r"a\[b\]c"


def test_full_xfade_graph_serializes_as_expected() -> None:
    g = FilterGraph()
    g.add(
        FilterNode(
            filter_name="format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS",
            inputs=["[0:v]"],
            outputs=["[v0]"],
        )
    )
    g.add(
        FilterNode(
            filter_name="format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS",
            inputs=["[1:v]"],
            outputs=["[v1]"],
        )
    )
    g.add(
        FilterNode(
            filter_name="xfade",
            inputs=["[v0]", "[v1]"],
            outputs=["[xv]"],
            args={"transition": "fade", "duration": "0.400", "offset": "4.600"},
        )
    )
    expected = (
        "[0:v]format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS[v0];"
        "[1:v]format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS[v1];"
        "[v0][v1]xfade=transition=fade:duration=0.400:offset=4.600[xv]"
    )
    assert g.serialize() == expected
