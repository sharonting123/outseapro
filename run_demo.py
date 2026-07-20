from __future__ import annotations

from soutui.catalog import sample_user
from soutui.commerce import CommerceEngine


def main() -> None:
    engine = CommerceEngine()
    user = sample_user()

    print("=== /search?q=跑鞋 ===")
    items, trace = engine.search(user, "跑鞋", page_size=10)
    if trace:
        for e in trace.events:
            print(f"  [{e.stage}] {e.title}")
            if e.formula:
                print(f"         ∴ {e.formula}")
    for item in items:
        tag = f"[{item.disclosure or '自然'}]"
        print(
            f"#{item.position} {tag:6s} {item.spu.title[:16]:16s} "
            f"{item.sku.attr_text():12s} ¥{item.sku.price:<7.0f} "
            f"spu={item.spu.spu_id} sku={item.sku.sku_id}"
        )

    print("\n=== /feed ===")
    items, _ = engine.feed(user, page_size=10)
    for item in items:
        tag = f"[{item.disclosure or '自然'}]"
        print(
            f"#{item.position} {tag:6s} {item.spu.title[:16]:16s} "
            f"{item.sku.attr_text():12s} ¥{item.sku.price:<7.0f}"
        )


if __name__ == "__main__":
    main()
