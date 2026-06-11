from datetime import datetime
from typing import Optional
import logging
import traceback

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from contextlib import asynccontextmanager
from pymongo import MongoClient
import os

from chatbot import (
    init_gemini, get_qdrant_client, ensure_collection,
    ingest_documents, retrieve, generate_answer
)

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

qdrant_client = None
mongo_client = None
history_collection = None


def get_mongo_client() -> MongoClient:
    uri = os.getenv('MONGODB_URI', 'mongodb://localhost:27017')
    return MongoClient(uri)


def init_mongo():
    global mongo_client, history_collection
    mongo_client = get_mongo_client()
    db = mongo_client['ira_rag_chatbot']
    history_collection = db['conversations']
    history_collection.create_index([('user_id', 1), ('created_at', -1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    global qdrant_client
    init_gemini()
    qdrant_client = get_qdrant_client()
    ensure_collection(qdrant_client)
    ingest_documents(qdrant_client)
    init_mongo()
    yield


app = FastAPI(
    title="IRA Skills RAG Chatbot API",
    description="Ask questions about IRA Skills policies.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "*",
            "Access-Control-Allow-Headers": "*",
        },
    )


class QueryRequest(BaseModel):
    question: str
    user_id: Optional[str] = None


class QueryResponse(BaseModel):
    answer: str
    sources: list[str]


class MessageRecord(BaseModel):
    role: str
    text: str
    created_at: datetime


class HistoryResponse(BaseModel):
    messages: list[MessageRecord]


def save_message(user_id: Optional[str], role: str, text: str):
    if not user_id or history_collection is None:
        return
    history_collection.insert_one({
        'user_id': user_id,
        'role': role,
        'text': text,
        'created_at': datetime.utcnow(),
    })


def get_user_history(user_id: str, limit: int = 10) -> list[dict]:
    docs = list(
        history_collection.find({'user_id': user_id})
        .sort('created_at', -1)
        .limit(limit)
    )
    docs.reverse()
    return [{'role': doc['role'], 'text': doc['text'], 'created_at': doc['created_at']} for doc in docs]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/history", response_model=HistoryResponse)
def history(user_id: str, limit: int = 20):
    if not user_id.strip():
        raise HTTPException(status_code=400, detail="user_id cannot be empty.")

    messages = get_user_history(user_id, limit)
    return HistoryResponse(messages=[MessageRecord(**message) for message in messages])


@app.post("/chat", response_model=QueryResponse)
def chat(req: QueryRequest):
    try:
        if not req.question.strip():
            raise HTTPException(status_code=400, detail="Question cannot be empty.")

        logger.info(f"Processing chat request: question='{req.question[:50]}...', user_id={req.user_id}")
        
        previous_chats = []
        if req.user_id:
            logger.info(f"Retrieving history for user {req.user_id}")
            previous_chats = get_user_history(req.user_id, limit=10)
            save_message(req.user_id, 'user', req.question)
            logger.info(f"Saved user message. History has {len(previous_chats)} previous messages.")

        logger.info("Retrieving relevant chunks from vector store")
        chunks = retrieve(qdrant_client, req.question)
        logger.info(f"Retrieved {len(chunks)} chunks from Qdrant")
        
        logger.info("Generating answer with Gemini")
        answer = generate_answer(req.question, chunks, previous_chats=previous_chats)
        logger.info(f"Answer generated: {answer[:100]}...")

        if req.user_id:
            save_message(req.user_id, 'assistant', answer)
            logger.info("Saved assistant message to history")

        seen = set()
        sources = [c['source'] for c in chunks if c['source'] not in seen and not seen.add(c['source'])]

        logger.info(f"Returning response with {len(sources)} sources")
        return QueryResponse(answer=answer, sources=sources)
    
    except HTTPException:
        raise
    except Exception as e:
        error_msg = f"Internal server error: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=error_msg)


@app.post("/reingest")
def reingest():
    ingest_documents(qdrant_client, force=True)
    return {"status": "re-ingestion complete"}
