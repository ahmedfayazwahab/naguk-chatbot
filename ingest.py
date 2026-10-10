"""Build the retrieval index: chunk each restaurant's JSON, embed the chunks
with Voyage, and upsert them into a Pinecone serverless index.

Chunking is per-section (hours / menu / description / logistics) rather than
fixed-length windows: at this corpus size (a dozen restaurants) section-level
chunks retrieve more precisely than arbitrary token windows, and there's no
long-form prose here that would need splitting further.
"""
import json
import os
import time
from pathlib import Path

import voyageai
from pinecone import Pinecone, ServerlessSpec

DATA_DIR = Path(__file__).parent / "data" / "restaurants"
EMBED_MODEL = "voyage-3-lite"
INDEX_NAME = os.environ.get("PINECONE_INDEX_NAME", "naguk-restaurants")

# Voyage AI throttles accounts with no payment method on file to 3 requests/min
# and 10K tokens/min. Batching keeps each request small and BATCH_PAUSE_SECONDS
# keeps us under 3 RPM — this is a one-time ingestion run, so slow-but-free is
# an easy trade against just adding a card.
BATCH_SIZE = 20
BATCH_PAUSE_SECONDS = 21

voyage = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])
pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])


def restaurant_chunks(r: dict) -> list[str]:
    chunks = []
    header = f"{r['name']} ({r.get('cuisine', '')}, {r.get('price_range', '')})"

    address_bits = [r.get("address", ""), r.get("city", ""), r.get("province", ""), r.get("postal_code", "")]
    full_address = ", ".join(b for b in address_bits if b)

    basics = [header]
    if full_address:
        basics.append(f"Address: {full_address}")
    if r.get("phone"):
        basics.append(f"Phone: {r['phone']}")
    if r.get("email"):
        basics.append(f"Email: {r['email']}")
    if r.get("status") == "upcoming":
        basics.append("This location has not opened yet (coming soon).")
    if r.get("description"):
        basics.append(r["description"])
    chunks.append(" ".join(basics))

    hours = r.get("hours", {})
    if any(v[0] for v in hours.values() if v):
        lines = [f"{header} hours:"]
        for day, (open_t, close_t) in hours.items():
            lines.append(f"{day}: {open_t} to {close_t}" if open_t and open_t != "closed" else f"{day}: closed")
        if r.get("hours_note"):
            lines.append(r["hours_note"])
        chunks.append(" ".join(lines))

    if r.get("menu_highlights"):
        chunks.append(f"{header} menu highlights: " + ", ".join(r["menu_highlights"]))
    elif r.get("menu_url"):
        chunks.append(f"{header} menu: see {r['menu_url']}")

    logistics = []
    if r.get("order_url"):
        logistics.append(f"Order online: {r['order_url']}")
    if r.get("reservation_url"):
        logistics.append(f"Reservations: {r['reservation_url']}")
    if r.get("gift_card_url"):
        logistics.append(f"Gift cards: {r['gift_card_url']}")
    if r.get("delivery_links"):
        logistics.append("Delivery: " + ", ".join(r["delivery_links"]))
    if logistics:
        chunks.append(f"{header} logistics: " + " ".join(logistics))

    if r.get("private_events"):
        # Phone and email are baked into this chunk's own text (not left to a
        # separate prompt rule) so that whenever search_knowledge retrieves it,
        # the model has them right there to relay — asking it to remember a
        # cross-cutting "always include email for private events" instruction
        # on top of everything else in the prompt proved unreliable in testing.
        private_line = (f"{header} hosts private events (celebrations, corporate events, "
                         f"mix and mingle gatherings, family gatherings). To book or ask "
                         f"about a private event, contact them directly.")
        if r.get("private_events_url"):
            private_line += f" Private events page: {r['private_events_url']}."
        if r.get("phone"):
            private_line += f" Phone: {r['phone']}."
        if r.get("email"):
            private_line += f" Email: {r['email']}."
        chunks.append(private_line)

    return chunks


def ensure_index(dimension: int):
    if INDEX_NAME not in [i.name for i in pc.list_indexes()]:
        pc.create_index(
            name=INDEX_NAME, dimension=dimension, metric="cosine",
            spec=ServerlessSpec(cloud="aws", region="us-east-1"),
        )
    return pc.Index(INDEX_NAME)


def main() -> None:
    records: list[dict] = []
    for path in sorted(DATA_DIR.glob("*.json")):
        if path.stem.startswith("_"):
            continue
        r = json.loads(path.read_text())
        for i, text in enumerate(restaurant_chunks(r)):
            records.append({"id": f"{r['slug']}-{i}", "restaurant": r["slug"], "text": text})

    texts = [rec["text"] for rec in records]
    embeddings: list[list[float]] = []
    batches = [texts[i:i + BATCH_SIZE] for i in range(0, len(texts), BATCH_SIZE)]
    for i, batch in enumerate(batches):
        result = voyage.embed(batch, model=EMBED_MODEL, input_type="document")
        embeddings.extend(result.embeddings)
        print(f"embedded batch {i + 1}/{len(batches)} ({len(batch)} chunks)")
        if i < len(batches) - 1:
            time.sleep(BATCH_PAUSE_SECONDS)

    index = ensure_index(dimension=len(embeddings[0]))
    vectors = [
        (rec["id"], emb, {"restaurant": rec["restaurant"], "text": rec["text"]})
        for rec, emb in zip(records, embeddings)
    ]
    index.upsert(vectors=vectors)
    print(f"upserted {len(vectors)} chunks into Pinecone index '{INDEX_NAME}'")


if __name__ == "__main__":
    main()
