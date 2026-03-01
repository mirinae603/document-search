import asyncio
import numpy as np

from extractor import DocumentExtractor
from embeddings import OpenRouterEmbeddings
from qa_agent import QAAgent

class SimpleVectorIndex:
    def __init__(self, embedder):
        self.embedder = embedder
        self.docs = []
        self.embeddings = []

    async def add_document(self, filename: str, text: str):
        print("🔹 Creating embedding for document...")
        embedding = await self.embedder.embed_text(text)

        self.docs.append({
            "file_id": str(len(self.docs)+1),
            "filename": filename,
            "file_path": filename,
            "text": text,
            "indexed_at": "now"
        })

        self.embeddings.append(embedding)
        print("Document indexed")

    async def hybrid_search(self, query: str, limit=3):
        print("🔹 Searching documents...")
        query_emb = await self.embedder.embed_text(query)

        sims = []
        for i, emb in enumerate(self.embeddings):
            sim = float(np.dot(query_emb, emb) /
                        (np.linalg.norm(query_emb) * np.linalg.norm(emb)))
            sims.append((sim, i))

        sims.sort(reverse=True)

        results = []
        for score, idx in sims[:limit]:
            doc = self.docs[idx]
            results.append({
                **doc,
                "rerank_score": score,
                "search_type": "vector"
            })

        print(f" Found {len(results)} relevant docs")
        return results



async def full_test():

    print("\n STARTING FULL PIPELINE TEST\n")

    extractor = DocumentExtractor(ocr_enabled=True)

    print("🔹 Running OCR on scanned.pdf...")
    with open("scanned.pdf", "rb") as f:
        content = f.read()

    extraction = await extractor.extract_text(content, "application/pdf")

    if not extraction["success"]:
        print("OCR FAILED:", extraction["error"])
        return

    text = extraction["text"]
    print("OCR complete")
    print("OCR text length:", len(text))
    print("Preview:\n", text[:500], "\n")

  
    embedder = OpenRouterEmbeddings(api_key="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")

    index = SimpleVectorIndex(embedder)

    await index.add_document("scanned.pdf", text)
    qa = QAAgent(
        searcher=index,
        seaweed_filer="unused",
        openrouter_key="YOUR_KEY"
    )
    question = "Summarize this document"

    print("\n🔹 Asking:", question, "\n")

    result = await qa.answer_question(question)

    print("\n================ ANSWER ================\n")
    print(result["answer"])

    print("\n================ METADATA ===============\n")
    print("Model:", result["metadata"].get("model"))
    print("Tokens:", result["metadata"].get("tokens_used"))
    print("Processing time:", result["metadata"].get("processing_time_seconds"), "sec")


if __name__ == "__main__":
    asyncio.run(full_test())