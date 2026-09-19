"""Guillotine-style (edge-to-edge) cutting layout engine.

Model
-----
* Every cut is a straight line spanning the full width or height of the
  sub-rectangle being cut (guillotine / 铡刀 rule).
* Every cut consumes ``kerf`` units of material (the blade/切缝 width).
  The kerf strip sits between the two children of a cut node.
* Pieces may be rotated 90 degrees.
* If the leftover after a cut would be narrower than the kerf it cannot
  yield any usable piece (the blade would eat it entirely), so it is
  treated as trim loss and not represented by a node.

The layout is built with the classic free-rectangle guillotine heuristic:
pieces are sorted largest-first and each piece is placed into the free
rectangle with the best short-side fit, splitting that rectangle with
guillotine cuts.  Because free rectangles are only ever produced by
splitting, the result is always expressible as a binary cut tree.
"""
from __future__ import annotations

from dataclasses import dataclass, field

EPS = 1e-9


class UnfitItemError(ValueError):
    """Raised when an item cannot fit on a fresh stock sheet in any orientation."""

    def __init__(self, item_id: str, width: float, height: float,
                 stock_width: float, stock_height: float) -> None:
        self.item_id = item_id
        super().__init__(
            f"item '{item_id}' ({width} x {height}) does not fit on stock "
            f"{stock_width} x {stock_height} in either orientation"
        )


@dataclass
class Piece:
    """One physical piece to place (an item expanded by its quantity)."""

    item_id: str
    width: float
    height: float


@dataclass
class Node:
    """A node of the binary cut tree.

    kind: "free" (usable offcut, during packing), "waste" (final offcut),
    "item" (a placed piece) or "cut" (internal node with two children).
    """

    kind: str
    x: float
    y: float
    width: float
    height: float
    direction: str | None = None  # cut nodes: "vertical" | "horizontal"
    children: list["Node"] | None = None
    piece: Piece | None = None    # item nodes
    rotated: bool = False         # item nodes


@dataclass
class Sheet:
    width: float
    height: float
    kerf: float
    root: Node
    free: list[Node] = field(default_factory=list)
    placements: list[dict] = field(default_factory=list)
    used_area: float = 0.0


# ---------------------------------------------------------------------------
# geometry helpers
# ---------------------------------------------------------------------------

def _fits(pw: float, ph: float, width: float, height: float) -> bool:
    return pw <= width + EPS and ph <= height + EPS


def _contains(a: Node, b: Node) -> bool:
    """True if rectangle a fully contains rectangle b."""
    return (
        a.x <= b.x + EPS
        and a.y <= b.y + EPS
        and a.x + a.width >= b.x + b.width - EPS
        and a.y + a.height >= b.y + b.height - EPS
    )


def _orientations(piece: Piece):
    """Yield (width, height) pairs, unrotated first."""
    yield piece.width, piece.height
    if piece.width != piece.height:
        yield piece.height, piece.width


# ---------------------------------------------------------------------------
# tree building
# ---------------------------------------------------------------------------

def _become_cut(node: Node, direction: str, children: list[Node]) -> None:
    node.kind = "cut"
    node.direction = direction
    node.children = children
    node.piece = None
    node.rotated = False


def _become_item(node: Node, item: Node) -> None:
    node.kind = "item"
    node.width = item.width
    node.height = item.height
    node.piece = item.piece
    node.rotated = item.rotated
    node.direction = None
    node.children = None


def _fill(node: Node, piece: Piece, pw: float, ph: float, kerf: float) -> list[Node]:
    """Place a piece (pw x ph) at the top-left corner of free leaf ``node``.

    Mutates ``node`` into an item leaf or a cut subtree and returns the new
    free rectangles created by the split.  Every split consumes ``kerf``
    material; remainders not wider than the kerf are discarded as trim loss.
    """
    x, y, w, h = node.x, node.y, node.width, node.height
    rotated = not (pw == piece.width and ph == piece.height)
    item = Node(kind="item", x=x, y=y, width=pw, height=ph,
                piece=piece, rotated=rotated)

    right_w = w - pw - kerf    # usable width right of a vertical cut
    bottom_h = h - ph - kerf   # usable height below a horizontal cut
    has_right = right_w > EPS
    has_bottom = bottom_h > EPS

    if not has_right and not has_bottom:
        # The piece consumes the whole rectangle (slivers <= kerf are trim).
        _become_item(node, item)
        return []

    if has_right and has_bottom:
        # Both offcuts are usable.  Two guillotine split orders are possible;
        # keep the one whose full-span offcut is larger.
        if right_w * h >= w * bottom_h:
            # Vertical cut first: right strip spans the full height, then a
            # horizontal cut separates the piece from the offcut below it.
            right = Node("free", x + pw + kerf, y, right_w, h)
            bottom = Node("free", x, y + ph + kerf, pw, bottom_h)
            strip = Node("cut", x, y, pw, h, direction="horizontal",
                         children=[item, bottom])
            _become_cut(node, "vertical", [strip, right])
            return [right, bottom]
        # Horizontal cut first, mirrored.
        right = Node("free", x + pw + kerf, y, right_w, ph)
        bottom = Node("free", x, y + ph + kerf, w, bottom_h)
        strip = Node("cut", x, y, w, ph, direction="vertical",
                     children=[item, right])
        _become_cut(node, "horizontal", [strip, bottom])
        return [bottom, right]

    if has_right:
        right = Node("free", x + pw + kerf, y, right_w, h)
        _become_cut(node, "vertical", [item, right])
        return [right]

    bottom = Node("free", x, y + ph + kerf, w, bottom_h)
    _become_cut(node, "horizontal", [item, bottom])
    return [bottom]


def _prune(free: list[Node], limit: int = 200) -> None:
    """Drop free rectangles fully contained in another (keeps packing fast).

    This never affects the guillotine property: it only removes redundant
    placement candidates.  Skipped for very large lists to bound the O(n^2)
    cost.
    """
    if len(free) > limit:
        return
    kept = []
    for i, rect in enumerate(free):
        dropped = False
        for j, other in enumerate(free):
            if i == j:
                continue
            # Drop rect if strictly inside another, or a duplicate of an
            # earlier one.
            if _contains(other, rect) and (not _contains(rect, other) or j < i):
                dropped = True
                break
        if not dropped:
            kept.append(rect)
    free[:] = kept


def _best_fit(sheets: list[Sheet], piece: Piece):
    """Find the best placement for a piece across all open sheets.

    Returns (sheet_index, free_rect_index, pw, ph) or None.  Scoring is
    best-short-side-fit with best-area-fit as tie-breaker.
    """
    best = None
    for si, sheet in enumerate(sheets):
        for fi, rect in enumerate(sheet.free):
            for pw, ph in _orientations(piece):
                if not _fits(pw, ph, rect.width, rect.height):
                    continue
                short_side = min(rect.width - pw, rect.height - ph)
                area_left = rect.width * rect.height - pw * ph
                score = (short_side, area_left)
                if best is None or score < best[0]:
                    best = (score, si, fi, pw, ph)
    if best is None:
        return None
    _, si, fi, pw, ph = best
    return si, fi, pw, ph


def _place(sheet: Sheet, free_idx: int, piece: Piece, pw: float, ph: float) -> None:
    rect = sheet.free.pop(free_idx)
    new_rects = _fill(rect, piece, pw, ph, sheet.kerf)
    sheet.free.extend(new_rects)
    _prune(sheet.free)
    sheet.used_area += pw * ph
    sheet.placements.append({
        "item": piece.item_id,
        "x": _num(rect.x),
        "y": _num(rect.y),
        "width": _num(pw),
        "height": _num(ph),
        "rotated": not (pw == piece.width and ph == piece.height),
    })


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------

def _num(value: float) -> float:
    """Round away floating point dust from coordinate arithmetic."""
    return round(value, 6)


def _finalize(node: Node) -> None:
    """Turn remaining free leaves into waste leaves."""
    if node.kind == "free":
        node.kind = "waste"
        return
    if node.kind == "cut":
        for child in node.children:
            _finalize(child)


def _node_dict(node: Node) -> dict:
    base = {
        "x": _num(node.x),
        "y": _num(node.y),
        "width": _num(node.width),
        "height": _num(node.height),
    }
    if node.kind == "cut":
        return {
            "type": "cut",
            "direction": node.direction,
            **base,
            "children": [_node_dict(child) for child in node.children],
        }
    if node.kind == "item":
        return {
            "type": "item",
            "item": node.piece.item_id,
            "rotated": node.rotated,
            **base,
        }
    return {"type": "waste", **base}


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def build_plan(stock_width: float, stock_height: float, kerf: float,
               items: list[tuple[str, float, float, int]]) -> dict:
    """Pack ``items`` (id, width, height, quantity) onto stock sheets.

    Returns a dict matching the PlanResponse schema.  Raises
    UnfitItemError if any item cannot fit on a fresh sheet.
    """
    pieces: list[Piece] = []
    for item_id, width, height, quantity in items:
        if not (_fits(width, height, stock_width, stock_height)
                or _fits(height, width, stock_width, stock_height)):
            raise UnfitItemError(item_id, width, height, stock_width, stock_height)
        pieces.extend(Piece(item_id, width, height) for _ in range(quantity))

    # Largest first: longer max side, then larger area.
    pieces.sort(key=lambda p: (max(p.width, p.height), p.width * p.height),
                reverse=True)

    sheets: list[Sheet] = []
    for piece in pieces:
        found = _best_fit(sheets, piece)
        if found is None:
            root = Node(kind="free", x=0.0, y=0.0,
                        width=stock_width, height=stock_height)
            sheet = Sheet(width=stock_width, height=stock_height, kerf=kerf,
                          root=root, free=[root])
            sheets.append(sheet)
            # A fresh sheet always fits (checked above); prefer unrotated.
            pw, ph = piece.width, piece.height
            if not _fits(pw, ph, stock_width, stock_height):
                pw, ph = ph, pw
            _place(sheet, 0, piece, pw, ph)
        else:
            si, fi, pw, ph = found
            _place(sheets[si], fi, piece, pw, ph)

    sheet_area = stock_width * stock_height
    result_sheets = []
    total_used = 0.0
    for index, sheet in enumerate(sheets):
        _finalize(sheet.root)
        total_used += sheet.used_area
        result_sheets.append({
            "index": index,
            "width": _num(stock_width),
            "height": _num(stock_height),
            "utilization": round(sheet.used_area / sheet_area, 6),
            "placements": sheet.placements,
            "cut_tree": _node_dict(sheet.root),
        })

    return {
        "stock": {"width": _num(stock_width), "height": _num(stock_height)},
        "kerf": _num(kerf),
        "sheets": result_sheets,
        "summary": {
            "sheet_count": len(sheets),
            "piece_count": len(pieces),
            "used_area": _num(total_used),
            "stock_area": _num(len(sheets) * sheet_area),
            "utilization": round(total_used / (len(sheets) * sheet_area), 6),
        },
    }
