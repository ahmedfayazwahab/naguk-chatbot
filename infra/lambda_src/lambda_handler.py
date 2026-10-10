"""API Gateway -> Lambda handler for the Naguk Group concierge agent.

Retrieval runs against a Pinecone serverless index (built by ingest.py).
Structured restaurant data (for the deterministic hours tool) is loaded from
S3 at cold start so it can be updated without redeploying the function. Each
request runs a short GPT function-calling loop (OpenAI) with three tools:
semantic search over the corpus, an open/closed calculator (which also
deterministically reports reservation info, since that was unreliable left to
the model alone), and a postal code based nearest-location ranker.

The client sends the full conversation history (not just the latest message)
on every request, so the agent is stateless server side but conversational
from the customer's point of view — no session store needed for a widget this
small.
"""
import base64
import io
import json
import math
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import boto3
from openai import OpenAI
from pinecone import Pinecone

S3_BUCKET = os.environ["INDEX_BUCKET"]
RATE_LIMIT_TABLE = os.environ["RATE_LIMIT_TABLE"]
PINECONE_INDEX_NAME = os.environ["PINECONE_INDEX_NAME"]
MODEL = "gpt-4o-mini"
EMBED_MODEL = "voyage-3-lite"
VOYAGE_EMBED_URL = "https://api.voyageai.com/v1/embeddings"
MAX_REQUESTS_PER_IP_PER_DAY = 30
MAX_HISTORY_MESSAGES = 10
TOP_K = 4

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
rate_table = dynamodb.Table(RATE_LIMIT_TABLE)

secrets = boto3.client("secretsmanager")
openai_client = OpenAI(
    api_key=secrets.get_secret_value(SecretId="naguk/openai-api-key")["SecretString"]
)
VOYAGE_API_KEY = secrets.get_secret_value(SecretId="naguk/voyage-api-key")["SecretString"]
pinecone_index = Pinecone(
    api_key=secrets.get_secret_value(SecretId="naguk/pinecone-api-key")["SecretString"]
).Index(PINECONE_INDEX_NAME)


def embed_query(text: str) -> list[float]:
    # Plain REST call instead of the voyageai SDK: that package unconditionally
    # requires numpy/tokenizers/pillow/langchain-text-splitters even for a single
    # remote embedding call, which alone pushed the Lambda bundle past 250MB.
    req = urllib.request.Request(
        VOYAGE_EMBED_URL,
        data=json.dumps({"input": [text], "model": EMBED_MODEL, "input_type": "query"}).encode(),
        headers={"Authorization": f"Bearer {VOYAGE_API_KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["data"][0]["embedding"]

_restaurants = None


def _load_restaurants():
    global _restaurants
    if _restaurants is not None:
        return
    s3.download_file(S3_BUCKET, "index/restaurants.json", "/tmp/restaurants.json")
    _restaurants = {r["slug"]: r for r in json.loads(open("/tmp/restaurants.json").read())}


def _check_rate_limit(ip: str) -> bool:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    key = f"{ip}#{today}"
    try:
        resp = rate_table.update_item(
            Key={"pk": key},
            UpdateExpression="ADD #c :one SET #ttl = if_not_exists(#ttl, :ttl)",
            ExpressionAttributeNames={"#c": "count", "#ttl": "expires_at"},
            ExpressionAttributeValues={
                ":one": 1,
                ":ttl": int((datetime.utcnow() + timedelta(days=2)).timestamp()),
            },
            ReturnValues="UPDATED_NEW",
        )
        return int(resp["Attributes"]["count"]) <= MAX_REQUESTS_PER_IP_PER_DAY
    except Exception:
        return True


def search_knowledge(query: str) -> str:
    q_vec = embed_query(query)
    result = pinecone_index.query(vector=q_vec, top_k=TOP_K, include_metadata=True)
    return "\n---\n".join(match["metadata"]["text"] for match in result["matches"])


EXTENSION_BY_MIME = {"ogg": "ogg", "mp4": "mp4", "m4a": "mp4", "wav": "wav", "webm": "webm"}


def transcribe_audio(audio_bytes: bytes, mime_type: str) -> str:
    # Fallback for browsers without the Web Speech API (Firefox, Safari): the
    # frontend records audio locally via MediaRecorder (broadly supported,
    # unlike SpeechRecognition) and uploads the clip here instead of
    # transcribing client side. Reuses the OpenAI key/client already configured
    # for chat, so no new vendor or API key for this — small per-use cost only
    # when this fallback path actually gets exercised, since most traffic uses
    # the free, instant, client side Web Speech API when the browser supports it.
    extension = next((ext for key, ext in EXTENSION_BY_MIME.items() if key in mime_type), "webm")
    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = f"recording.{extension}"
    result = openai_client.audio.transcriptions.create(model="whisper-1", file=audio_file)
    return result.text


def _contact_details_note(r: dict) -> str:
    # Appended to every check_hours answer so reservation info, phone, and a
    # website link are always correct by construction, rather than depending on
    # the model separately calling search_knowledge and choosing to mention
    # them — that was tried for reservations alone and was unreliable with this
    # model (skipped the extra call most of the time). Email is deliberately
    # left out here: it should only come up if the customer asks for it.
    parts = []
    if r.get("reservation_url"):
        parts.append(f"Reservations: {r['reservation_url']}")
    else:
        parts.append("This location does not take reservations; it is walk in only.")
    if r.get("phone"):
        parts.append(f"Phone: {r['phone']}")
    if r.get("menu_url"):
        parts.append(f"Website: {r['menu_url']}")
    return " " + " ".join(parts)


def check_hours(restaurant_slug: str, day: str | None = None) -> str:
    r = _restaurants.get(restaurant_slug)
    if not r:
        return f"No restaurant found matching '{restaurant_slug}'."
    if r.get("status") == "upcoming":
        return f"{r['name']} has not opened yet."

    tz = ZoneInfo(r.get("timezone", "Asia/Bangkok"))
    now = datetime.now(tz)
    day_key = day or now.strftime("%a").lower()[:3]
    open_t, close_t = r.get("hours", {}).get(day_key, ["closed", "closed"])
    if open_t == "closed":
        return f"{r['name']} is closed on {day_key}."
    if day is None:
        current = now.strftime("%H:%M")
        is_open = open_t <= current <= close_t
        status = "open" if is_open else "closed"
        return f"{r['name']} is currently {status} (today's hours: {open_t} to {close_t}).{_contact_details_note(r)}"
    return f"{r['name']} hours on {day_key}: {open_t} to {close_t}.{_contact_details_note(r)}"


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_km = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((phi2 - phi1) / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * earth_radius_km * math.asin(math.sqrt(a))


_postal_geocode_cache: dict[str, tuple[float, float]] = {}


def _geocode_postal_code(postal_code: str) -> tuple[float, float] | None:
    # Nominatim (OpenStreetMap) is free and keyless, unlike a paid geocoding API,
    # consistent with this project's zero added cost approach. OSM's coverage of
    # full six character Canadian postal codes is patchy, so fall back to the
    # three character area prefix, which it knows far more reliably. Only
    # successes are cached: caching a failure would turn one transient network
    # blip into a wrong answer for the rest of this container's life.
    compact = postal_code.strip().upper().replace(" ", "")
    if compact in _postal_geocode_cache:
        return _postal_geocode_cache[compact]
    for query in dict.fromkeys([compact, compact[:3]]):
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {"q": f"{query}, Canada", "format": "json", "limit": 1, "countrycodes": "ca"})
        req = urllib.request.Request(url, headers={"User-Agent": "naguk-chatbot/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
        except Exception as exc:
            print(f"geocoding {query} failed: {exc}")
            continue
        if data:
            coords = (float(data[0]["lat"]), float(data[0]["lon"]))
            _postal_geocode_cache[compact] = coords
            return coords
    return None


def find_nearest(postal_code: str | None = None, latitude: float | None = None,
                  longitude: float | None = None, private_events_only: bool = False) -> str:
    # Real distance, not an approximation: every open restaurant carries
    # latitude/longitude (added once by geocode.py), the customer's position
    # comes straight from shared coordinates or from geocoding their typed
    # postal code, and results are ranked by haversine kilometres. Phone and
    # website are appended here, in code, so every recommendation carries them
    # without depending on the model remembering to include them.
    if latitude is None or longitude is None:
        if not postal_code:
            return "I need either a postal/zip code or a shared location to find the nearest restaurant."
        if len(postal_code.strip().replace(" ", "")) < 3:
            return "That doesn't look like a full postal code. Could you double check it?"
        coords = _geocode_postal_code(postal_code)
        if not coords:
            return ("I couldn't place that postal code on the map. Could you double check it, "
                    "or use the share location button instead?")
        latitude, longitude = coords

    candidates = [
        r for r in _restaurants.values()
        if r.get("status") != "upcoming"
        and r.get("latitude") is not None and r.get("longitude") is not None
        and (r.get("private_events") or not private_events_only)
    ]
    if not candidates:
        return ("I couldn't find any matching locations. Here is the full location list "
                "instead, grouped by brand:\n" + _managed_locations_summary())

    ranked = sorted(
        ((_haversine_km(latitude, longitude, r["latitude"], r["longitude"]), r) for r in candidates),
        key=lambda pair: pair[0],
    )[:5]

    lines = []
    for km, r in ranked:
        line = f"{r['name']}, located at {r.get('address', 'address unavailable')}, {r.get('city', '')}, about {km:.1f} km away."
        if r.get("phone"):
            line += f" Phone: {r['phone']}."
        if r.get("menu_url"):
            line += f" Website: {r['menu_url']}"
        if private_events_only:
            # Private events are the one case where the customer needs email as
            # well as phone, so it is added here rather than left to the prompt.
            if r.get("email"):
                line += f" Email: {r['email']}."
            if r.get("private_events_url"):
                line += f" Private events page: {r['private_events_url']}"
        lines.append(line)

    qualifier = " that host private events" if private_events_only else ""
    return f"Closest locations{qualifier}, nearest first:\n" + "\n".join(lines)



def _managed_locations_summary() -> str:
    # Bullets use "*" rather than "-": this text is sometimes relayed close to
    # verbatim (e.g. find_nearest's fallback), and the user does not want hyphens
    # anywhere in the chatbot's output, including as list punctuation.
    by_brand: dict[str, list[str]] = {}
    cuisines: dict[str, str] = {}
    for r in _restaurants.values():
        brand = r.get("brand", r["name"])
        by_brand.setdefault(brand, []).append(f"{r['name']} (slug: {r['slug']})")
        if r.get("cuisine"):
            cuisines[brand] = r["cuisine"]
    lines = []
    for brand, names in sorted(by_brand.items()):
        cuisine_part = f" [{cuisines[brand]}]" if brand in cuisines else ""
        lines.append(f"* {brand}{cuisine_part}: {', '.join(sorted(names))}")
    return "\n".join(lines)


def _build_tools() -> list[dict]:
    # restaurant_slug is constrained to an enum of the exact slugs that exist, built fresh
    # per-request from _restaurants — without this the model has to guess a slug from the
    # restaurant's display name (e.g. "thairoomgrand_yorkmills" vs the real
    # "thairoomgrand-yorkmills"), and an unmatched guess makes check_hours report "not found"
    # even though the restaurant is right there in the data.
    return [
        {
            "type": "function",
            "function": {
                "name": "search_knowledge",
                "description": "Semantic search over restaurant descriptions, menus, addresses, and logistics for the Naguk Group restaurants.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "check_hours",
                "description": "Deterministically check whether a specific restaurant is open right now, or its hours on a given day. Always use this for open/closed questions instead of guessing from retrieved text.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "restaurant_slug": {"type": "string", "enum": sorted(_restaurants.keys())},
                        "day": {"type": "string", "description": "e.g. 'mon', 'tue'. Omit for 'right now'."},
                    },
                    "required": ["restaurant_slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_nearest",
            "description": "Ranks Naguk Group locations by closeness to the customer, using EITHER a postal/zip code OR a shared latitude and longitude (from the frontend's share-location button) — provide whichever one you have, never both. Use this whenever a customer wants the nearest/closest location.",
            "parameters": {
                "type": "object",
                "properties": {
                    "postal_code": {"type": "string", "description": "Use this when the customer typed a postal or zip code."},
                    "latitude": {"type": "number", "description": "Use together with longitude when the customer shared their location instead of typing a postal code."},
                    "longitude": {"type": "number"},
                    "private_events_only": {"type": "boolean", "description": "Set true when the customer is asking which restaurants host or take private events/bookings; restricts results to locations that do."},
                },
            },
        },
    },
]

SYSTEM_PROMPT_TEMPLATE = """You are the concierge for Naguk Group, a collection of restaurants.
The customer may be continuing an earlier part of this same conversation, so use
that prior context (e.g. a restaurant named a few messages ago) instead of
re-asking something you were already told.

NEVER use a hyphen or dash of any kind (-, en dash, em dash) anywhere in your
reply, including inside words, number ranges, as a separator in a list item, or
as a list bullet marker. Rewrite instead, for example "walk in only" not
"walk-in only", "11 AM to 11 PM" not "11am-11pm", and when listing several
restaurants use a colon or just "is located at" between the name and its
details, never a dash (write "thairoomgrand YorkMills is located at 861
Yorkmills Road", not "thairoomgrand YorkMills - 861 Yorkmills Road"). When you
list multiple items, use a numbered list (1. 2. 3.) or plain commas, never a
dash as the bullet character (not "- thairoomgrand Downtown"). The one
exception is a link or other factual value a tool gave you verbatim (a URL, an
address, a postal code) — copy those exactly as given even if they contain a
hyphen; never edit a link.

For ANY question about a specific restaurant — hours, address, menu, reservations,
delivery, gift cards, private events, or anything else — you MUST call
search_knowledge BEFORE answering. For open/closed questions, also call
check_hours — its answer already includes that restaurant's reservation info,
phone number, and website link, so always relay those alongside the hours
rather than leaving them out. Never reply "I don't have information" or guess
from your own general knowledge without searching first; the answer is very
likely in the tool results even if it isn't obvious from the question's
wording. Only skip calling tools for messages that are clearly unrelated to any
restaurant (greetings, chit-chat). If, after searching, the tools genuinely
return nothing relevant, say so and suggest the customer call the restaurant
directly. Never state whether a restaurant takes reservations unless a tool
result actually said so — guessing has caused wrong answers before. Do not
proactively mention a restaurant's email address, even if you can see one in
the search results, unless the customer specifically asks for an email or a
way to email the restaurant directly — EXCEPT for private events (see below),
where email is always relevant and should be included. Keep answers short and
conversational.

Whenever you recommend, suggest, or point the customer toward any specific
restaurant, always include its phone number and a website link (the menu link)
in that same reply, along with whatever other details are relevant to what they
asked, so they have enough to act on without having to ask again.

When a customer wants to book or ask about a private event, always give them
that restaurant's phone number AND email address (this is the one exception to
the no email rule above) alongside the private events link, so they have more
than one way to reach the restaurant directly rather than only a web form.

Remember that most customers do not know what Naguk Group is and rarely have a
specific restaurant in mind when they first ask something broad — "what's the
best restaurant", "what do you recommend", "what's good", or similar browsing
style questions. For these, do not just ask a bare clarifying question with
nothing to go on, and do not dump the full location list either. Instead:
first briefly mention a couple of genuinely different options from the list
below (for example a Thai option, the family style restaurant, and the sports
bar) so they have something concrete to react to; then ask one or two specific,
concrete questions to figure out what they actually want, such as what cuisine
they are in the mood for, what the occasion is (quick bite, family dinner, date
night, group event), what area is convenient for them, or roughly how many
people — pick whichever question or two seems most useful, do not ask all of
them at once. Once you understand what they want, recommend a specific
restaurant following the paragraph above (phone number and website link
included).

When you end a reply about a specific restaurant with an offer to keep helping,
make it concrete by naming one relevant thing they might want next (for example
"Would you like to see their menu or ask about private events?") rather than
only a generic "What else can I help you with?". Skip this closing offer for
messages that aren't about a specific restaurant (e.g. greetings, or when
you've just told the customer Naguk Group doesn't manage a location they asked
about).

If a customer wants the nearest or closest location to them, check first
whether their message already contains a shared location (it will look like
raw coordinates, e.g. "my location is latitude 43.7, longitude -79.4") or a
postal/zip code. If it contains coordinates, call find_nearest with latitude
and longitude. If it contains a postal/zip code, call find_nearest with that.
If it has neither, ask them to either share their location using the button
near the message box, or type their postal/zip code — whichever they give you,
call find_nearest with the matching argument. Relay the results plainly; never
read coordinates back to the customer, since they are not meaningful to a
person. If find_nearest can't match anything nearby, fall back to listing the
available locations instead of leaving them with nothing.

Distance matters a lot to customers — a restaurant that matches what they
asked for but is far away is a poor recommendation, so treat it as the primary
factor whenever you are choosing between multiple possible restaurants to
suggest, not only when the customer explicitly says "nearest." A message may
include a note that the customer already shared their location earlier in this
session (coordinates, carried forward automatically) — when present, use it
for ANY recommendation in that reply, even one about a feature, occasion
or cuisine, not only explicit nearest requests: call find_nearest
with those coordinates and let proximity drive which option(s) you lead with.
When the customer asks which restaurants host or take private events or
private bookings, call find_nearest with private_events_only set to true, so
the list only contains locations that do, ordered by distance.
Relay find_nearest results in the order given and only answer what was asked;
do not append extra lists (for example private events) the customer did not
ask for.
If a customer asks you to recommend or suggest restaurants based on a feature,
occasion, or cuisine and you do not already know their location this session,
ask for their postal/zip code or invite them to use the share location button
near the message box BEFORE giving a list — do not recommend purely by feature
match while ignoring distance, since that has produced poor suggestions before
(e.g. recommending a far away restaurant over a closer one that fits just as
well). The one time you can skip asking for location is when the customer is
asking about one specific restaurant they already named, not a recommendation
among several.

If a customer's spelling of a restaurant name is unclear, missing spaces, or
could plausibly mean more than one of the locations below, still try
search_knowledge with your best interpretation of what they meant. If you are
genuinely unsure which specific restaurant they mean, ask a short clarifying
question that names the one or two most likely candidates (e.g. "did you mean
thairoomgrand?") rather than guessing silently or saying you have no
information, and use their answer to continue.

Several brands below (thairoomgrand, Gabby's, Eat BKK, MMV Mumbaiya, Koh Lipe
Thai Kitchen, Somtum Jinda) have more than one location. If a customer asks for
a location specific detail — address, phone, hours, which one is closest, and
so on — without saying which one they mean, do not list all of that brand's
locations with invented or placeholder details for each (never write something
like "exact address not provided" as a stand in for a real value you didn't
look up). Instead ask which location they mean, naming the options from the
list below, then call search_knowledge or check_hours for that one specific
location once they answer. Only answer about the brand as a whole, without
asking which location, for genuinely brand wide questions that are not
location specific, such as what cuisine it serves.

Naguk Group manages ONLY the following locations, grouped by brand (cuisine
shown in brackets where known):
{managed_locations}

Some of these brands have other locations elsewhere (other cities, other malls,
other neighbourhoods) that are run independently and are NOT part of Naguk
Group. If a customer asks about a location of one of these brands that is not
in the list above, do not answer using your own general knowledge of that
brand — instead tell them clearly that Naguk Group does not manage/operate
that particular location, and suggest they look up that location directly."""


TOOL_FUNCTIONS = {
    "search_knowledge": search_knowledge,
    "check_hours": check_hours,
    "find_nearest": find_nearest,
}

BULLET_HYPHEN_RE = re.compile(r"(?m)^(\s*)-(\s+)")


def _strip_bullet_hyphens(text: str) -> str:
    # The model doesn't reliably follow the "no hyphen bullets" instruction every
    # single time (observed recurring even after several prompt rewrites) — this
    # is just list-marker punctuation, not user data, so enforce it deterministically
    # as a safety net rather than relying on prompting alone to be fully reliable.
    return BULLET_HYPHEN_RE.sub(r"\1*\2", text)


PRIVATE_INTENT_RE = re.compile(r"private|event|book|party|parties|celebrat|corporate|banquet|gathering", re.I)
NEAREST_EMAIL_RE = re.compile(r"^(.+?), located at .*?Email: (\S+@\S+?)\.(?:\s|$)", re.M)


def _recent_user_text(history: list[dict], n: int = 2) -> str:
    user_msgs = [m.get("content", "") for m in history if m.get("role") == "user"]
    return " ".join(user_msgs[-n:])


def _append_missing_emails(reply: str, emails: list[tuple[str, str]]) -> str:
    # gpt-4o-mini intermittently drops the email from a private events answer
    # even when the tool result contains it (prompt rules proved unreliable for
    # this), so any returned email for a location the reply mentions but whose
    # address is absent gets appended here, in code.
    missing = [(n, e) for n, e in emails if n in reply and e not in reply]
    if not missing:
        return reply
    return reply + "\n\nEmail for private events:\n" + "\n".join(f"* {n}: {e}" for n, e in missing)


def run_agent(history: list[dict]) -> str:
    # `history` is the prior user/assistant turns plus the new user message, as
    # sent by the client — only ever user/assistant text, never tool-call
    # scaffolding. Each call rebuilds its own tool-calling loop from scratch on
    # top of that history; the loop's internal tool_calls/tool messages are not
    # persisted back to the client, only the final text reply is.
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(managed_locations=_managed_locations_summary())
    messages = [{"role": "system", "content": system_prompt}, *history]
    private_emails: list[tuple[str, str]] = []

    for _ in range(4):
        response = openai_client.chat.completions.create(
            model=MODEL, max_tokens=1024, tools=_build_tools(), messages=messages,
        )
        message = response.choices[0].message
        if response.choices[0].finish_reason != "tool_calls":
            return _strip_bullet_hyphens(_append_missing_emails(message.content or "", private_emails))

        messages.append(message.model_dump(exclude_none=True))
        for tool_call in message.tool_calls:
            args = json.loads(tool_call.function.arguments)
            fn = TOOL_FUNCTIONS.get(tool_call.function.name)
            try:
                if (tool_call.function.name == "find_nearest" and args.get("private_events_only")
                        and not PRIVATE_INTENT_RE.search(_recent_user_text(history))):
                    result = ("The customer did not ask about private events, so do not mention "
                              "them. Use the earlier nearest results as they are.")
                else:
                    result = fn(**args) if fn else "Unknown tool."
                    if tool_call.function.name == "find_nearest":
                        private_emails.extend(NEAREST_EMAIL_RE.findall(result))
            except Exception as exc:
                # A transient failure here (e.g. Voyage AI's free tier rate limit,
                # hit at this project's current traffic burst tolerance) must not
                # crash the whole request with an ugly 500 that also skips the
                # CORS headers set below — tell the model the lookup failed so it
                # can be honest with the customer instead of guessing or stalling.
                print(f"tool {tool_call.function.name} failed: {exc}")
                result = (f"Tool '{tool_call.function.name}' failed right now due to a temporary "
                          "technical issue. Do not guess or make up an answer; tell the customer "
                          "there is a brief technical issue and ask them to try again shortly.")
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": result})

    return "Sorry, I'm having trouble answering that right now."


ALLOWED_ORIGINS = set(os.environ.get("ALLOWED_ORIGINS", "").split(","))


def _cors_origin(event) -> str:
    # API Gateway HTTP API (v2) lowercases incoming header names in the event.
    request_origin = event.get("headers", {}).get("origin", "")
    return request_origin if request_origin in ALLOWED_ORIGINS else next(iter(ALLOWED_ORIGINS), "*")


def handler(event, context):
    # The Lambda is wired as a catch-all $default route (every method, including
    # OPTIONS), which takes priority over API Gateway's built-in CORS preflight
    # handling — so the browser's preflight OPTIONS request lands here too and
    # must be answered directly with a 2xx, or browsers abort the real request.
    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return {
            "statusCode": 204,
            "headers": {
                "Access-Control-Allow-Origin": _cors_origin(event),
                "Access-Control-Allow-Methods": "POST",
                "Access-Control-Allow-Headers": "content-type",
            },
        }

    cors_headers = {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": _cors_origin(event),
    }

    ip = event.get("requestContext", {}).get("http", {}).get("sourceIp", "unknown")
    if not _check_rate_limit(ip):
        return {"statusCode": 429, "headers": cors_headers,
                "body": json.dumps({"error": "Rate limit exceeded, try again tomorrow."})}

    body = json.loads(event.get("body") or "{}")
    path = event.get("requestContext", {}).get("http", {}).get("path", "")

    if path.rstrip("/").endswith("/transcribe"):
        audio_b64 = body.get("audio")
        if not audio_b64:
            return {"statusCode": 400, "headers": cors_headers,
                    "body": json.dumps({"error": "audio is required"})}
        try:
            text = transcribe_audio(base64.b64decode(audio_b64), body.get("mimeType", "audio/webm"))
        except Exception as exc:
            print(f"transcription failed: {exc}")
            return {"statusCode": 500, "headers": cors_headers,
                    "body": json.dumps({"error": "Could not transcribe audio, please try again."})}
        return {"statusCode": 200, "headers": cors_headers, "body": json.dumps({"text": text})}

    _load_restaurants()

    raw_history = body.get("messages")
    if not isinstance(raw_history, list) or not raw_history:
        return {"statusCode": 400, "headers": cors_headers,
                "body": json.dumps({"error": "messages (a non-empty array) is required"})}

    history = [
        {"role": m["role"], "content": m["content"].strip()[:2000]}
        for m in raw_history
        if isinstance(m, dict) and m.get("role") in ("user", "assistant")
        and isinstance(m.get("content"), str) and m["content"].strip()
    ][-MAX_HISTORY_MESSAGES:]
    if not history or history[-1]["role"] != "user":
        return {"statusCode": 400, "headers": cors_headers,
                "body": json.dumps({"error": "the last message must be from the user"})}

    try:
        reply = run_agent(history)
    except Exception as exc:
        # Any truly unexpected failure still needs CORS headers on its response,
        # or the browser reports a confusing "Network error" on top of the real
        # problem — an uncaught exception here would otherwise skip straight to
        # API Gateway's bare error format with no headers at all.
        print(f"run_agent crashed: {exc}")
        return {"statusCode": 500, "headers": cors_headers,
                "body": json.dumps({"error": "Something went wrong, please try again."})}

    return {
        "statusCode": 200,
        "headers": {
            **cors_headers,
        },
        "body": json.dumps({"reply": reply}),
    }
