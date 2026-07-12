from __future__ import annotations


def paginate_items(
    items: list,
    *,
    page,
    per_page,
    allowed_per_page: tuple[int, ...] = (10, 25, 50, 100),
    default_per_page: int = 25,
) -> tuple[list, dict]:
    total = len(items)
    per_page_value = _coerce_positive_int(per_page, default_per_page)
    if per_page_value not in allowed_per_page:
        per_page_value = default_per_page

    page_count = max(1, (total + per_page_value - 1) // per_page_value)
    page_value = min(_coerce_positive_int(page, 1), page_count)
    start_index = (page_value - 1) * per_page_value
    end_index = min(start_index + per_page_value, total)
    window_start = max(1, page_value - 2)
    window_end = min(page_count, window_start + 4)
    window_start = max(1, window_end - 4)

    return items[start_index:end_index], {
        "page": page_value,
        "per_page": per_page_value,
        "allowed_per_page": list(allowed_per_page),
        "total": total,
        "page_count": page_count,
        "has_prev": page_value > 1,
        "has_next": page_value < page_count,
        "prev_page": max(1, page_value - 1),
        "next_page": min(page_count, page_value + 1),
        "start": start_index + 1 if total else 0,
        "end": end_index,
        "page_numbers": list(range(window_start, window_end + 1)),
    }


def _coerce_positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
