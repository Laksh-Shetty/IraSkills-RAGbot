import os
import time
import hashlib
from typing import Optional
from dotenv import load_dotenv

from google import genai
from google.genai import types
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct
)
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.markdown import Markdown

load_dotenv()

console = Console()

GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY", "")
QDRANT_URL       = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY   = os.getenv("QDRANT_API_KEY", "")
COLLECTION_NAME  = "ira_skills_docs"
EMBEDDING_MODEL  = os.getenv("EMBEDDING_MODEL", "gemini-embedding-001")
CHAT_MODEL       = "gemini-2.5-flash"
CHUNK_SIZE       = 600
CHUNK_OVERLAP    = 100
TOP_K            = 5
EMBED_DIM        = 3072

gemini_client = None


def init_gemini():
    global gemini_client
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY not set in .env")
    gemini_client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(api_version="v1"),
    )


def embed_text(text: str) -> list[float]:
    result = gemini_client.models.embed_content(
        model=EMBEDDING_MODEL,
        contents=text,
        config=types.EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT"),
    )
    return result.embeddings[0].values


def embed_query(query: str) -> list[float]:
    result = gemini_client.models.embed_content(
        model=EMBEDDING_MODEL,
        contents=query,
        config=types.EmbedContentConfig(task_type="RETRIEVAL_QUERY"),
    )
    return result.embeddings[0].values


def get_qdrant_client() -> QdrantClient:
    if QDRANT_API_KEY:
        return QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    if QDRANT_URL == ":memory:":
        console.print("[yellow]⚠  Using in-memory Qdrant — vectors will be lost on exit.[/yellow]")
        return QdrantClient(":memory:")
    return QdrantClient(url=QDRANT_URL)


def ensure_collection(client: QdrantClient):
    existing = [c.name for c in client.get_collections().collections]
    if COLLECTION_NAME not in existing:
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
        )
        console.print(f"[green]✓ Created Qdrant collection '{COLLECTION_NAME}'[/green]")
        return

    collection_info = client.get_collection(collection_name=COLLECTION_NAME)
    existing_size = collection_info.config.params.vectors.size
    if existing_size != EMBED_DIM:
        console.print(
            f"[yellow]⚠  Existing collection '{COLLECTION_NAME}' is configured for dim {existing_size}, "
            f"but EMBED_DIM is {EMBED_DIM}. Recreating collection.[/yellow]"
        )
        client.delete_collection(collection_name=COLLECTION_NAME)
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
        )
        console.print(f"[green]✓ Recreated Qdrant collection '{COLLECTION_NAME}' with dim {EMBED_DIM}[/green]")
    else:
        console.print(f"[blue]ℹ  Collection '{COLLECTION_NAME}' already exists[/blue]")


def chunk_text(text: str, source: str) -> list[dict]:
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        chunk = text[start:end].strip()
        if len(chunk) > 50:
            chunks.append({"text": chunk, "source": source})
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def fetch_page(url: str) -> str:
    import requests
    from bs4 import BeautifulSoup

    headers = {"User-Agent": "Mozilla/5.0 (compatible; IRASkillsBot/1.0)"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        console.print(f"[red]  ✗ Failed to fetch {url}: {e}[/red]")
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header",
                      "aside", "noscript", "form", "button"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return "\n".join(lines)


SOURCES = {
    "Terms of Service":  "https://iraskills.ai/terms-of-service/",
    "Privacy Policy":    "https://iraskills.ai/privacy-policy/",
    "Refund Policy":     "https://iraskills.ai/refund/",
    "Contact Us":        "https://iraskills.ai/contact-us/",
    "Blog":              "https://iraskills.ai/blog/",
}


def ingest_documents(client: QdrantClient, force: bool = False):
    count = client.count(collection_name=COLLECTION_NAME).count
    if count > 0 and not force:
        console.print(
            f"[blue]ℹ  Collection already has {count} vectors. "
            "Skipping ingestion (use --reingest to force).[/blue]"
        )
        return

    all_chunks: list[dict] = []

    console.print("\n[bold]📥 Fetching documents…[/bold]")
    for name, url in SOURCES.items():
        console.print(f"  • {name}: {url}")
        text = fetch_page(url)
        if text:
            chunks = chunk_text(text, source=name)
            all_chunks.extend(chunks)
            console.print(f"    → {len(chunks)} chunks")
        time.sleep(1)

    console.print(f"\n[bold]🔢 Embedding {len(all_chunks)} chunks…[/bold]")
    points: list[PointStruct] = []
    for i, chunk in enumerate(all_chunks):
        vector = embed_text(chunk["text"])
        uid = int(hashlib.md5(f"{chunk['source']}-{i}".encode()).hexdigest(), 16) % (2**63)
        points.append(PointStruct(
            id=uid,
            vector=vector,
            payload={"text": chunk["text"], "source": chunk["source"]},
        ))
        if (i + 1) % 10 == 0:
            console.print(f"  … {i + 1}/{len(all_chunks)}")
        time.sleep(0.1)

    console.print(f"\n[bold]💾 Upserting {len(points)} vectors into Qdrant…[/bold]")
    batch_size = 50
    for start in range(0, len(points), batch_size):
        client.upsert(
            collection_name=COLLECTION_NAME,
            points=points[start : start + batch_size],
        )
    console.print(f"[green]✓ Ingestion complete! {len(points)} vectors stored.[/green]")



def retrieve(client: QdrantClient, query: str) -> list:
    results = client.query_points(
        collection_name=COLLECTION_NAME,
        query=embed_query(query),
        limit=TOP_K,
        with_payload=True,
    ).points
    return [{"text": r.payload["text"], "source": r.payload["source"], "score": r.score}
            for r in results]


SYSTEM_PROMPT = """You are a helpful assistant for IRA Skills, an online learning platform.
Answer questions ONLY based on the provided context from IRA Skills policy documents.
If the context does not contain enough information to answer, say so politely and suggest
the user visit https://iraskills.ai or contact support.
Do not make up information. Be concise, friendly, and accurate."""


def generate_answer(query: str, chunks: list[dict], previous_chats: Optional[list[dict]] = None) -> str:
    if not chunks:
        return ("I'm sorry, I couldn't find relevant information in the IRA Skills "
                "knowledge base to answer your question. Please visit "
                "https://iraskills.ai or contact support for help.")

    history_text = ''
    if previous_chats:
        history_lines = []
        for msg in previous_chats:
            role = msg['role'].capitalize()
            history_lines.append(f"[{role}] {msg['text']}")
        history_text = "\n".join(history_lines)

    context_parts = []
    for c in chunks:
        context_parts.append(f"[Source: {c['source']}]\n{c['text']}")
    context = "\n\n---\n\n".join(context_parts)

    prompt = f"""{SYSTEM_PROMPT}

"""
    if history_text:
        prompt += f"PREVIOUS CHATS:\n{history_text}\n\n"
    prompt += f"CONTEXT:\n{context}\n\nUSER QUESTION:\n{query}\n\nANSWER:"

    response = gemini_client.models.generate_content(
        model=CHAT_MODEL,
        contents=prompt,
    )
    return response.text.strip()


def chat_loop(client: QdrantClient):
    console.print(Panel.fit(
        "[bold cyan]IRA Skills Policy Assistant[/bold cyan]\n"
        "Ask me anything about IRA Skills' Terms of Service, Privacy Policy,\n"
        "Refund Policy, Contact info, or Blog.\n"
        "Type [bold]'exit'[/bold] or [bold]'quit'[/bold] to leave.",
        border_style="cyan",
    ))

    while True:
        console.print()
        query = Prompt.ask("[bold green]You[/bold green]").strip()

        if not query:
            continue
        if query.lower() in {"exit", "quit", "bye"}:
            console.print("[yellow]Goodbye! 👋[/yellow]")
            break

        with console.status("[bold blue]Searching knowledge base…[/bold blue]"):
            chunks = retrieve(client, query)

        with console.status("[bold blue]Generating answer…[/bold blue]"):
            answer = generate_answer(query, chunks)

        console.print()
        console.print(Panel(
            Markdown(answer),
            title="[bold cyan]IRA Skills Assistant[/bold cyan]",
            border_style="cyan",
        ))

        seen = set()
        sources = [c["source"] for c in chunks if c["source"] not in seen and not seen.add(c["source"])]
        console.print(f"[dim]📚 Sources: {', '.join(sources)}[/dim]")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="IRA Skills RAG Chatbot")
    parser.add_argument("--reingest", action="store_true",
                        help="Force re-scraping and re-embedding of all documents")
    args = parser.parse_args()

    console.print("[bold]🚀 Starting IRA Skills RAG Chatbot…[/bold]\n")

    init_gemini()
    client = get_qdrant_client()
    ensure_collection(client)
    ingest_documents(client, force=args.reingest)

    chat_loop(client)


if __name__ == "__main__":
    main()
