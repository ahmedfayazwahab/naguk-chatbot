"""Render each restaurant JSON into a single human-readable PDF.

JSON stays the source of truth (used by ingest.py and the check_hours tool).
The PDF is a byproduct: something a customer/reviewer can open directly,
and a second document format that exercises the ingestion pipeline's
PDF-parsing path so it isn't only ever tested against clean JSON.
"""
import json
from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

DATA_DIR = Path(__file__).parent / "data" / "restaurants"
OUT_DIR = Path(__file__).parent / "data" / "pdfs"
DAY_NAMES = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
             "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}


def render(restaurant: dict, out_path: Path) -> None:
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(out_path), pagesize=LETTER)
    title = restaurant["name"] if not restaurant.get("brand") else f"{restaurant['brand']} — {restaurant['name']}"
    story = [Paragraph(title, styles["Title"]), Spacer(1, 12)]

    if restaurant.get("status") == "upcoming":
        story.append(Paragraph("Status: Opening soon", styles["Heading3"]))

    full_address = restaurant.get("address", "")
    city_line = ", ".join(p for p in [restaurant.get("city"), restaurant.get("province")] if p)
    if restaurant.get("postal_code"):
        city_line = f"{city_line} {restaurant['postal_code']}".strip()
    if city_line:
        full_address = f"{full_address}, {city_line}" if full_address else city_line

    for label, value in [("Cuisine", restaurant.get("cuisine")), ("Price range", restaurant.get("price_range")),
                          ("Address", full_address), ("Phone", restaurant.get("phone")),
                          ("Email", restaurant.get("email"))]:
        if value:
            story.append(Paragraph(f"<b>{label}:</b> {value}", styles["Normal"]))

    if restaurant.get("description"):
        story.append(Spacer(1, 8))
        story.append(Paragraph(restaurant["description"], styles["Normal"]))

    hours = restaurant.get("hours", {})
    if any(hours.values()):
        story.append(Spacer(1, 12))
        story.append(Paragraph("Hours", styles["Heading2"]))
        for day, label in DAY_NAMES.items():
            open_t, close_t = hours.get(day, ["", ""])
            line = f"{label}: {open_t}–{close_t}" if open_t and open_t != "closed" else f"{label}: Closed"
            story.append(Paragraph(line, styles["Normal"]))
        if restaurant.get("hours_note"):
            story.append(Paragraph(f"<i>{restaurant['hours_note']}</i>", styles["Normal"]))

    if restaurant.get("menu_highlights"):
        story.append(Spacer(1, 12))
        story.append(Paragraph("Menu highlights", styles["Heading2"]))
        for item in restaurant["menu_highlights"]:
            story.append(Paragraph(f"• {item}", styles["Normal"]))
    elif restaurant.get("menu_url"):
        story.append(Spacer(1, 12))
        story.append(Paragraph(f"Menu: {restaurant['menu_url']}", styles["Normal"]))

    story.append(Spacer(1, 12))
    story.append(Paragraph("Links", styles["Heading2"]))
    for label, key in [("Order online", "order_url"), ("Reservations", "reservation_url"),
                       ("Gift cards", "gift_card_url")]:
        if restaurant.get(key):
            story.append(Paragraph(f"{label}: {restaurant[key]}", styles["Normal"]))

    if restaurant.get("private_events"):
        story.append(Spacer(1, 12))
        story.append(Paragraph("Private events", styles["Heading2"]))
        story.append(Paragraph(
            "This location hosts private events (celebrations, corporate events, mix & mingle "
            "gatherings, family get-togethers).", styles["Normal"]))
        if restaurant.get("private_events_url"):
            story.append(Paragraph(f"Book an event: {restaurant['private_events_url']}", styles["Normal"]))

    doc.build(story)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(DATA_DIR.glob("*.json")):
        if path.stem.startswith("_"):
            continue
        restaurant = json.loads(path.read_text())
        out_path = OUT_DIR / f"{restaurant['slug']}.pdf"
        render(restaurant, out_path)
        print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
