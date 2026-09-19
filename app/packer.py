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
* Locked placements (pre-positioned pieces) are validated to form a
  layout that can be recursively decomposed into guillotine cuts with
  the given kerf; the remaining quantities are then packed only into
  the usable offcut leaves of that tree (and fresh sheets as needed).

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


class LockedPlacementError(ValueError):
    """Raised when locked placements are invalid (unknown item, out of
    bounds, overlapping, exceeding the demand) or cannot be decomposed
    into a guillotine cut tree with the given kerf."""


@dataclass
class Piece:
    """One physical piece to place (an item expanded by its quantity)."""

    item_id: str
    width: float
    height: float


@dataclass
class LockedRect:
    """One locked placement resolved to absolute sheet coordinates."""

    index: int          # 1-based position in the request's locked list
    item_id: str
    x: float
    y: float
    width: float        # placed size (already swapped when rotated)
    height: float
    rotated: bool
    piece: Piece        # original (unrotated) item dimensions


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
# locked placements
# ---------------------------------------------------------------------------

# Safety net bounding the number of subproblems evaluated while searching
# for a guillotine decomposition of the locked placements.
MAX_DECOMPOSE_STEPS = 100_000


def _validate_locked(locked: list[tuple[str, float, float, bool]],
                     item_dims: dict[str, tuple[float, float]],
                     quantities: dict[str, int],
                     stock_width: float, stock_height: float) -> list[LockedRect]:
    """Resolve and validate locked placements against the item list.

    Every locked piece must reference a known item, lie inside the stock,
    not overlap any other locked piece, and the locked count of an item
    must not exceed its demand (locked pieces count toward the demand).
    """
    rects: list[LockedRect] = []
    counts: dict[str, int] = {}
    for position, (item_id, lx, ly, rotated) in enumerate(locked, start=1):
        if item_id not in item_dims:
            raise LockedPlacementError(
                f"locked placement #{position} references unknown item "
                f"'{item_id}'"
            )
        width, height = item_dims[item_id]
        pw, ph = (height, width) if rotated else (width, height)
        if (lx < -EPS or ly < -EPS
                or lx + pw > stock_width + EPS
                or ly + ph > stock_height + EPS):
            raise LockedPlacementError(
                f"locked placement #{position} (item '{item_id}') at "
                f"(x={lx}, y={ly}) with size {pw} x {ph} exceeds stock "
                f"{stock_width} x {stock_height}"
            )
        counts[item_id] = counts.get(item_id, 0) + 1
        if counts[item_id] > quantities[item_id]:
            raise LockedPlacementError(
                f"locked placements for item '{item_id}' exceed its "
                f"quantity {quantities[item_id]}"
            )
        rects.append(LockedRect(index=position, item_id=item_id, x=lx, y=ly,
                                width=pw, height=ph, rotated=rotated,
                                piece=Piece(item_id, width, height)))

    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            overlap_x = min(a.x + a.width, b.x + b.width) - max(a.x, b.x)
            overlap_y = min(a.y + a.height, b.y + b.height) - max(a.y, b.y)
            if overlap_x > EPS and overlap_y > EPS:
                raise LockedPlacementError(
                    f"locked placements #{a.index} (item '{a.item_id}') and "
                    f"#{b.index} (item '{b.item_id}') overlap"
                )
    return rects


def _decompose_locked_layout(rects: list[LockedRect], stock_width: float,
                             stock_height: float, kerf: float):
    """Build a cut tree whose item leaves are exactly the locked pieces.

    Returns ``(root, conflicts)``.  ``root`` is the tree root (its free
    leaves are the usable offcuts) or ``None`` when the locked layout
    cannot be produced by guillotine cuts with the given kerf; in that
    case ``conflicts`` holds the index tuples of the sub-layouts that
    could not be decomposed.

    A region is ``(x, y, x2, y2, right_exact, bottom_exact)``.  Content
    must always be flush with the region's left/top edges (a cut node
    places both children at its origin / right after the kerf band).  An
    "exact" far edge is a cut line: an item leaf must match it exactly,
    because the parent's next sibling starts exactly ``kerf`` after it.
    A non-exact far edge may fall short by up to ``kerf`` (trim loss,
    mirroring how ``_fill`` lets a piece consume a free rectangle).
    """
    conflicts: list[tuple[int, ...]] = []
    failed: set = set()
    steps = 0

    def edge_ok(rect_edge: float, region_edge: float, exact: bool) -> bool:
        if exact:
            return abs(rect_edge - region_edge) <= EPS
        return -EPS <= region_edge - rect_edge <= kerf + EPS

    def decompose(region, sub) -> Node | None:
        nonlocal steps
        steps += 1
        if steps > MAX_DECOMPOSE_STEPS:
            raise LockedPlacementError(
                "locked layout is too complex to verify as guillotine cuts"
            )
        x, y, x2, y2, right_exact, bottom_exact = region
        if not sub:
            return Node(kind="free", x=x, y=y, width=x2 - x, height=y2 - y)
        key = (region, tuple(r.index for r in sub))
        if key in failed:
            return None
        if len(sub) == 1:
            r = sub[0]
            if (abs(r.x - x) <= EPS and abs(r.y - y) <= EPS
                    and edge_ok(r.x + r.width, x2, right_exact)
                    and edge_ok(r.y + r.height, y2, bottom_exact)):
                return Node(kind="item", x=r.x, y=r.y, width=r.width,
                            height=r.height, piece=r.piece, rotated=r.rotated)
        # Try every candidate guillotine cut.  It suffices to cut right
        # after a piece's far edge or so that the kerf band ends at a
        # piece's near edge: any valid cut can be shifted to such a
        # position without changing which pieces it separates.
        for direction in ("vertical", "horizontal"):
            if direction == "vertical":
                cuts = sorted({r.x + r.width for r in sub}
                              | {r.x - kerf for r in sub})
            else:
                cuts = sorted({r.y + r.height for r in sub}
                              | {r.y - kerf for r in sub})
            for c in cuts:
                near, far = (x, x2) if direction == "vertical" else (y, y2)
                if c - near <= EPS or far - c - kerf <= EPS:
                    continue
                first_sub, second_sub = [], []
                for r in sub:
                    start = r.x if direction == "vertical" else r.y
                    end = start + (r.width if direction == "vertical"
                                   else r.height)
                    if end <= c + EPS:
                        first_sub.append(r)
                    elif start >= c + kerf - EPS:
                        second_sub.append(r)
                    else:
                        break  # piece straddles the kerf band: invalid cut
                else:
                    if direction == "vertical":
                        first_region = (x, y, c, y2, True, False)
                        second_region = (c + kerf, y, x2, y2, False, False)
                    else:
                        first_region = (x, y, x2, c, False, True)
                        second_region = (x, c + kerf, x2, y2, False, False)
                    first = decompose(first_region, tuple(first_sub))
                    if first is None:
                        continue
                    second = decompose(second_region, tuple(second_sub))
                    if second is None:
                        continue
                    return Node(kind="cut", x=x, y=y, width=x2 - x,
                                height=y2 - y, direction=direction,
                                children=[first, second])
        failed.add(key)
        conflicts.append(tuple(r.index for r in sub))
        return None

    root = decompose((0.0, 0.0, stock_width, stock_height, False, False),
                     tuple(rects))
    return root, conflicts


def _collect_free(node: Node, out: list[Node]) -> None:
    """Collect the free (usable offcut) leaves of a decomposed layout."""
    if node.kind == "free":
        out.append(node)
    elif node.kind == "cut":
        for child in node.children:
            _collect_free(child, out)


def _locked_sheet(rects: list[LockedRect], stock_width: float,
                  stock_height: float, kerf: float) -> Sheet:
    """Build the first sheet from validated locked placements."""
    root, conflicts = _decompose_locked_layout(rects, stock_width,
                                               stock_height, kerf)
    if root is None:
        by_index = {r.index: r for r in rects}
        worst = min(conflicts, key=lambda c: (len(c), c))
        detail = ", ".join(
            f"#{i} (item '{by_index[i].item_id}', x={by_index[i].x}, "
            f"y={by_index[i].y}, rotated={by_index[i].rotated})"
            for i in worst
        )
        raise LockedPlacementError(
            f"locked placements cannot be decomposed into a guillotine cut "
            f"tree with kerf {kerf}; conflicting placements: {detail}"
        )
    free: list[Node] = []
    _collect_free(root, free)
    return Sheet(
        width=stock_width,
        height=stock_height,
        kerf=kerf,
        root=root,
        free=free,
        placements=[
            {
                "item": r.item_id,
                "x": _num(r.x),
                "y": _num(r.y),
                "width": _num(r.width),
                "height": _num(r.height),
                "rotated": r.rotated,
                "locked": True,
            }
            for r in rects
        ],
        used_area=sum(r.width * r.height for r in rects),
    )


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
        "locked": False,
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
               items: list[tuple[str, float, float, int]],
               locked: list[tuple[str, float, float, bool]] | None = None
               ) -> dict:
    """Pack ``items`` (id, width, height, quantity) onto stock sheets.

    ``locked`` optionally lists pre-positioned pieces (id, x, y, rotated)
    for the first sheet.  Locked pieces count toward the demand of their
    item; the layout is validated and decomposed into a guillotine cut
    tree first, then the remaining quantities are packed into the usable
    offcut leaves of that tree (and fresh sheets as needed).

    Returns a dict matching the PlanResponse schema.  Raises
    UnfitItemError if any item cannot fit on a fresh sheet and
    LockedPlacementError if the locked placements are invalid.
    """
    item_dims: dict[str, tuple[float, float]] = {}
    quantities: dict[str, int] = {}
    for item_id, width, height, quantity in items:
        if not (_fits(width, height, stock_width, stock_height)
                or _fits(height, width, stock_width, stock_height)):
            raise UnfitItemError(item_id, width, height, stock_width,
                                 stock_height)
        item_dims[item_id] = (width, height)
        quantities[item_id] = quantities.get(item_id, 0) + quantity

    locked_rects = _validate_locked(locked or [], item_dims, quantities,
                                    stock_width, stock_height)
    locked_count: dict[str, int] = {}
    for rect in locked_rects:
        locked_count[rect.item_id] = locked_count.get(rect.item_id, 0) + 1

    pieces: list[Piece] = []
    for item_id, width, height, quantity in items:
        remaining = quantity - locked_count.get(item_id, 0)
        pieces.extend(Piece(item_id, width, height) for _ in range(remaining))

    # Largest first: longer max side, then larger area.
    pieces.sort(key=lambda p: (max(p.width, p.height), p.width * p.height),
                reverse=True)

    sheets: list[Sheet] = []
    if locked_rects:
        sheets.append(_locked_sheet(locked_rects, stock_width, stock_height,
                                    kerf))

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
            "piece_count": len(pieces) + len(locked_rects),
            "used_area": _num(total_used),
            "stock_area": _num(len(sheets) * sheet_area),
            "utilization": round(total_used / (len(sheets) * sheet_area), 6),
        },
    }
