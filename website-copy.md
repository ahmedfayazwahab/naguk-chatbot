# Naguk Group Concierge: an AI assistant for a restaurant group

## What it is

The Naguk Group Concierge is an AI chatbot that answers questions about a group of 11 restaurant brands and 28 open locations around the Greater Toronto Area, with 4 more locations marked as opening soon. You can ask it things like "Is Kao Kang open on Friday night?", "Which restaurants do private events near me?" or "Where can I take my kids for dinner?" and it answers using real, collected restaurant information, not guesses.

It was built as a portfolio project to show how a modern AI assistant can be grounded in real data, kept inside clear boundaries, and run cheaply in the cloud.

## What it can do

* Answer questions about addresses, phone numbers, opening hours, menus, reservations, online ordering, gift cards and private events.
* Tell you if a location is open right now, using Toronto time.
* Find the closest restaurants to you. You can share your location with one button, or type your postal code. Results are ranked by real distance in kilometres.
* Find locations that host private events, nearest first, with the phone, email and events page.
* Recommend places when you are undecided, by asking about cuisine, occasion, area or group size.
* Remember the conversation, so you can ask "what is their address?" and then "can I make a reservation there?"
* Listen to you. The Speak button turns your voice into text, in every major browser.
* Stay honest. It only talks about locations that the group actually manages. If you ask about another branch of the same brand elsewhere, it tells you that location is not part of the group. It says so when a location has not opened yet.

## How it works, in plain words

1. You type or speak a question on the page.
2. The page sends your conversation to a small program running in the cloud.
3. That program gives your question to a large language model (the AI that writes the reply), together with a set of rules and three tools it can use to look things up.
4. The AI decides which tool it needs, looks up the facts, and writes a reply based only on what it found.
5. The reply is shown in the chat, with clickable links.

The key idea is that the AI does not answer from memory. It looks the information up first. This approach is called Retrieval Augmented Generation, or RAG, and it is why the answers stay accurate and do not invent details.

## The technology, step by step

**1. Collecting the data**
The restaurant information was gathered from each restaurant's own website, location by location: address, phone, hours, menu link, reservation link, ordering link, gift cards, private events and email. Anything a site did not publish was left blank instead of guessed. Each location is stored as a structured JSON file, and a PDF copy is generated for each one.

**2. Chunking and embeddings (turning text into numbers)**
Each location's information is split into small, focused pieces of text called chunks: basic details, hours, menu, logistics, and private events. Each chunk is sent to an embedding model, Voyage AI's voyage-3-lite, which converts the text into a long list of numbers called a vector. Texts with similar meaning get similar vectors. This is what lets the system understand that "somewhere to celebrate a birthday" is related to "private events."

**3. The vector database**
The vectors are stored in Pinecone, a vector database (serverless, on AWS). The index holds 132 chunks. When you ask a question, your question is also converted into a vector, and Pinecone returns the chunks whose meaning is closest to it. This is semantic search: it matches meaning, not just exact words, so it still works if you phrase things differently or misspell a name.

**4. The language model and tools**
The reply is written by OpenAI's gpt-4o-mini. It uses function calling: instead of answering directly, it can ask the system to run one of three tools.
* Search knowledge: semantic search over the Pinecone index, for menus, private events, reservations and general questions.
* Check hours: reads the structured data and works out whether a location is open now, in Toronto time.
* Find nearest: calculates the real distance (the haversine great circle formula) from you to every open location, using coordinates stored for each one, and returns them nearest first. A typed postal code is converted to a position using OpenStreetMap's free lookup service.

The model's results are combined into the final answer in a loop, up to four rounds, until it has what it needs.

**5. Rules and guardrails**
A system prompt gives the assistant its role and rules: always look things up first, stay on topic, only discuss locations the group manages, always include a phone number and website when recommending a place, ask for your location before suggesting by features, and never show raw coordinates. The list of managed locations is loaded fresh from the data on every request, so adding a restaurant never needs a code change.

**6. Rules enforced in code, not just in the prompt**
Language models do not follow every instruction every time. Testing showed this clearly, so the important rules are enforced by ordinary code:
* Reservation, phone and website details are added by code to every hours answer.
* The "private events" filter is applied only when you actually ask about events or bookings.
* Private event emails are added automatically if the AI forgets them.
* The tool is restricted to real location IDs, so the AI cannot invent one.
* List formatting is cleaned up by code.

**7. Memory and your shared location**
The server stores nothing about you. Your browser keeps the chat and sends the recent messages with every question, so the assistant can follow the conversation. If you share your location, the page keeps it for the whole session and quietly attaches it to every question, while the chat only shows "📍 Shared my location."

**8. Voice input**
In Chrome and Edge, your browser's built in speech recognition is used. In Firefox and Safari, which lack it, the page records your voice and sends it to OpenAI's Whisper speech to text model. Click once to start, click again to stop and send.

**9. The cloud setup (AWS)**
There is no server running all day. The backend is serverless.
* The page itself is a static file served from Amazon S3 through CloudFront.
* Amazon API Gateway is the public entry point, limited to a few requests per second and to calls from this website.
* AWS Lambda (Python 3.12) runs the assistant's code only while a question is being answered.
* Amazon DynamoDB keeps a simple daily limit of 30 messages per visitor, to control cost and abuse.
* AWS Secrets Manager holds the API keys safely, outside the code.
* An AWS Budget sends an email alert if monthly spending goes over a small limit.
* The whole setup is defined as code with AWS CDK (Python), so it can be rebuilt or changed repeatably.

**10. The chat page**
The chat window is a single self contained web page written in plain HTML, CSS and JavaScript, with no framework. It shows a typing indicator while waiting, turns links into clickable links, and safely escapes any text so nothing unexpected can run in the page.

## Technology list

* Language model: OpenAI gpt-4o-mini (function calling)
* Embeddings: Voyage AI voyage-3-lite
* Vector database: Pinecone (serverless)
* Speech to text: OpenAI Whisper, plus the browser Web Speech API
* Location: browser Geolocation API, OpenStreetMap Nominatim, haversine distance
* Backend: AWS Lambda (Python 3.12), API Gateway, DynamoDB, S3, Secrets Manager, CloudFront, AWS Budgets
* Infrastructure as code: AWS CDK (Python)
* Frontend: HTML, CSS and JavaScript

## What I learned

* Retrieval makes the difference between a chatbot that sounds right and one that is right.
* Small models skip instructions some of the time, so anything that must always happen belongs in code, with the prompt for the rest.
* Real distance beats approximations. An early version ranked by postal code area and sometimes suggested a far away restaurant. Switching to true distance fixed it.
* Serverless keeps a public demo close to free while still being safe, thanks to rate limits, budget alerts and secrets kept out of the code.

## A note on the data

The restaurant information was collected from each restaurant's public website, and details such as hours or menus can change. For anything important, such as a reservation or a large booking, please confirm with the restaurant directly using the phone number the assistant provides.
