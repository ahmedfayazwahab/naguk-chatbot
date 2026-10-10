# Naguk Group Concierge

**An AI assistant for a restaurant group, built on RAG, tool calling and serverless AWS.**

### [Try it live: ahmedfayazwahab.ca/chat.html](https://ahmedfayazwahab.ca/chat.html)

The Concierge answers questions about **11 restaurant brands and 28 open locations** around the Greater Toronto Area (another 4 are marked as opening soon). Ask it things like *"Is Kao Kang open on Friday night?"*, *"Which restaurants do private events near me?"* or *"Where can I take my kids for dinner?"*

The answers come from real, collected restaurant data, not from the model's guesses. This is a portfolio project: it shows how an AI assistant can use real data, stay inside clear boundaries, and run cheaply in the cloud.

## What it can do

- Answer questions about addresses, phone numbers, hours, menus, reservations, online ordering, gift cards and private events.
- Say whether a location is open right now, in Toronto time.
- Find the closest restaurants, ranked by real distance in kilometres, from a shared location or a typed postal code.
- Find locations that host private events, nearest first, with phone, email and events page.
- Recommend places by asking about cuisine, occasion, area or group size.
- Remember the conversation, so "what's their address?" followed by "can I reserve there?" works.
- Take voice input in every major browser.
- Stay honest: it only talks about locations the group manages, and says so when a location hasn't opened yet.

## How it works

```
 You type or speak
        │
        ▼
 chat page (static HTML) ──POST──► API Gateway ──► Lambda (Python 3.12)
                                                      │
                          ┌───────────────────────────┤
                          ▼                           ▼
                 gpt-4o-mini with 3 tools      DynamoDB (daily limit)
                  ├─ search_knowledge ──► Voyage embed ──► Pinecone (semantic search)
                  ├─ check_hours      ──► restaurants.json (S3), Toronto time
                  └─ find_nearest     ──► haversine distance (+ Nominatim for postal codes)
                          │
                          ▼
              reply written only from what the tools returned
```

1. You type or speak a question.
2. The page sends the recent conversation to a small program in the cloud.
3. That program gives the question to a language model, along with a set of rules and three lookup tools.
4. The model picks a tool, looks up the facts, and writes a reply based only on what it found.
5. The reply appears in the chat, with clickable links.

The model doesn't answer from memory. It looks things up first. This is Retrieval Augmented Generation (RAG), and it is why the answers stay accurate.

## The technology

| Piece | What it does | Used here |
|---|---|---|
| Language model | Writes replies, decides which tool to call (function calling) | OpenAI `gpt-4o-mini` |
| Embeddings | Turns text chunks and questions into vectors | Voyage AI `voyage-3-lite` |
| Vector database | Semantic search over the chunks | Pinecone (serverless) |
| Speech to text | Voice input | Browser Web Speech API; OpenAI `whisper-1` fallback for browsers without it |
| Location | Distance and postal codes | Browser Geolocation API, OpenStreetMap Nominatim, haversine formula |
| Backend | Runs the agent only while a question is answered | AWS Lambda (Python 3.12), API Gateway |
| Limits and secrets | Rate limiting, API keys, cost alerts | DynamoDB, Secrets Manager, AWS Budgets |
| Infrastructure as code | The whole stack in one file | AWS CDK (Python) |
| Frontend | Single page, no framework | HTML, CSS, JavaScript |

## Repository layout

```
.
├── data/
│   ├── restaurants/        # one JSON file per location: the source of truth (+ _template.json)
│   └── pdfs/               # human-readable PDF copy of each location (generated)
├── RAG Docx/               # raw collected source documents per brand
├── ingest.py               # chunk -> embed (Voyage) -> upsert into Pinecone
├── geocode.py              # adds latitude/longitude to each open location (Nominatim)
├── generate_pdf.py         # renders each JSON into a PDF
├── publish_index.sh        # bundles the JSONs and uploads restaurants.json to S3
├── lambda_handler.py       # symlink -> infra/lambda_src/lambda_handler.py
├── frontend/
│   └── chat-widget.html    # the chat page (served as /chat.html on ahmedfayazwahab.ca)
├── infra/
│   ├── app.py, stack.py    # CDK app and the NagukChatbotStack
│   ├── lambda_src/         # the Lambda code + its deliberately minimal requirements
│   ├── deploy-iam-policy.json
│   └── cdk.json
├── requirements.txt        # local tooling (ingest, PDFs)
└── website-copy.md         # plain-language write-up used on the portfolio site
```

## The workflow

There are two loops: **data** (what the assistant knows) and **code** (what it does). They are independent, so updating a menu never needs a redeploy.

### 1. Data pipeline

```
collect from each restaurant's site ─► data/restaurants/*.json ─► geocode.py ─► generate_pdf.py
                                                                       │
                                        ┌──────────────────────────────┴───────────────┐
                                        ▼                                              ▼
                                   ingest.py                                    publish_index.sh
                       chunk, embed, upsert to Pinecone              restaurants.json -> S3 (for hours / nearest)
```

1. **Collect.** Facts come from each restaurant's own website, location by location. Anything a site didn't publish is left blank instead of guessed. One JSON file per location (schema in `data/restaurants/_template.json`).
2. **Geocode** (`python geocode.py`). Adds coordinates to every open location using OpenStreetMap's free Nominatim (1 request per second, no key). Run again only when a location is added or moves.
3. **Render PDFs** (`python generate_pdf.py`). A readable copy of each location. JSON stays the source of truth.
4. **Index** (`python ingest.py`). Each location is split into section-level chunks (basics, hours, menu, logistics, private events), embedded with `voyage-3-lite`, and upserted into Pinecone by id, so re-running overwrites in place. It batches and pauses to stay under Voyage's free-tier rate limit.
5. **Publish** (`./publish_index.sh <index-bucket>`). Bundles all location JSONs into `restaurants.json` and uploads it to S3. The deterministic tools (`check_hours`, `find_nearest`) read this file; the Lambda refreshes it on its next cold start.

### 2. Code and infrastructure

```bash
cd infra
pip install aws-cdk-lib aws-cdk.aws-lambda-python-alpha constructs
npm install -g aws-cdk          # the CDK CLI, not the Python library
cdk bootstrap                   # first time only, per account/region
cdk deploy                      # note the ApiUrl and IndexBucketName outputs
```

The stack creates the Lambda, a throttled HTTP API with CORS locked to the site, the DynamoDB rate-limit table, the S3 index bucket, IAM access to the three secrets, and a monthly budget alarm.

### 3. Frontend

`frontend/chat-widget.html` is one static page. Set `API_URL` at the top of its script to the `ApiUrl` stack output, then host it anywhere on an allowed origin. On ahmedfayazwahab.ca it is uploaded to S3 as `chat.html` and served through CloudFront:

```bash
aws s3 cp frontend/chat-widget.html s3://<site-bucket>/chat.html
aws cloudfront create-invalidation --distribution-id <distribution-id> --paths "/chat.html"
```

CORS only allows `https://ahmedfayazwahab.ca` and `http://localhost:8080` (see `ALLOWED_ORIGINS` in `infra/stack.py`). To test locally:

```bash
cd frontend && python3 -m http.server 8080
# open http://localhost:8080/chat-widget.html
```

### 4. Updating restaurant info later

Edit the JSON, then re-run `python ingest.py` and `./publish_index.sh <index-bucket>`. No redeploy is needed. To pick up the new `restaurants.json` immediately instead of on the next cold start, touch an environment variable with `aws lambda update-function-configuration`.

## Set up from scratch

You need accounts for OpenAI (chat model and Whisper), Voyage AI (embeddings) and Pinecone (vector store), and an AWS account.

1. Create a **separate IAM user** for deployment and configure it as a named CLI profile (`aws configure --profile <your-profile>`). `infra/deploy-iam-policy.json` is the policy this project was deployed with; review and tighten it before reuse.
2. Store the three API keys in Secrets Manager. The names must match what the Lambda reads:
   ```bash
   aws secretsmanager create-secret --name naguk/openai-api-key   --secret-string "sk-proj-..."
   aws secretsmanager create-secret --name naguk/voyage-api-key   --secret-string "pa-..."
   aws secretsmanager create-secret --name naguk/pinecone-api-key --secret-string "pcsk_..."
   ```
3. Install local tooling and build the index:
   ```bash
   pip install -r requirements.txt
   VOYAGE_API_KEY=pa-... PINECONE_API_KEY=pcsk_... python ingest.py
   ```
   `ingest.py` creates the Pinecone serverless index on first run.
4. Deploy (section 2), publish the index (section 1, step 5), then host the frontend (section 3).

No keys are stored in this repository. They are read from Secrets Manager (Lambda) or environment variables (`ingest.py`).

## Guardrails and cost control

- **Rules in the prompt and in code.** A system prompt tells the assistant to look things up first, stay on topic and discuss only managed locations. Small models don't follow every instruction every time, so the rules that must always hold run as ordinary code: reservation, phone and website details on hours answers, the private-events filter, automatic private-events emails, and restricting tools to real location ids. The list of managed locations is loaded on each request, so adding a restaurant needs no code change.
- **Stateless server.** The server stores nothing about visitors. The browser sends the last 10 messages with each question; a shared location is kept for the session and attached quietly.
- **Limits.** 30 requests per visitor (by IP) per day, enforced in DynamoDB; API Gateway throttled to 5 requests per second (burst 10); a monthly AWS Budgets alarm (`BUDGET_LIMIT_USD` in `stack.py`).
- **Nothing runs all day.** Lambda only runs while a question is being answered, which keeps a public demo close to free.

## Design notes

- `infra/lambda_src/requirements.txt` is **deliberately minimal** (`openai`, `pinecone`). The `voyageai` SDK pulls in numpy, tokenizers and pillow even for a single call, which pushed the bundle past Lambda's 250 MB limit, so `embed_query()` calls Voyage's REST API directly with `urllib`. `boto3` is omitted because the Lambda runtime provides it.
- `infra/lambda_src/` exists so CDK bundles only the handler. Pointing `entry` at the project root swept `cdk.out`'s own earlier build output back into the bundle, producing ever-deeper paths until Docker failed with "File name too long".
- Section-level chunks (rather than fixed-size windows) retrieve more precisely at this corpus size, and there is no long-form prose that needs splitting further.

## What I learned

- Retrieval is the difference between a chatbot that sounds right and one that is right.
- Anything that must always happen belongs in code. The prompt handles the rest.
- Real distance beats approximations. An early version ranked by postal-code area and sometimes suggested a far-away restaurant.
- Serverless keeps a public demo close to free, with rate limits, budget alerts and secrets kept out of the code.

## A note on the data

The information comes from each restaurant's public website, with synthetic or collected summaries. It only contains business details a restaurant already publishes (addresses, phone numbers, hours, menus, business emails). Hours and menus can change, so for anything important, confirm with the restaurant using the phone number the assistant provides. There is no automated test suite yet.

## Author

**Ahmed Fayaz Wahab**: [ahmedfayazwahab.ca](https://ahmedfayazwahab.ca) · [LinkedIn](https://linkedin.com/in/ahmedfayazwahab) · [GitHub](https://github.com/ahmedfayazwahab)

## License

No open-source license has been applied yet, so all rights are reserved by default. The restaurant data describes businesses that belong to their respective owners.
