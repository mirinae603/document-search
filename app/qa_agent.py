import logging
import asyncio
import re
from typing import List, Dict, Optional
from datetime import datetime
from openai import AsyncOpenAI


logger = logging.getLogger(__name__)


class QAAgent:
    """Advanced Q&A Agent — document-grounded or general LLM mode"""

    def __init__(self, searcher, seaweed_filer: str, openrouter_key: str):
        self.searcher = searcher
        self.seaweed_filer = seaweed_filer
        self.client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=openrouter_key
        )
        self.models = [
            "stepfun/step-3.5-flash:free",
            "google/gemini-2.0-flash-exp:free",
            "meta-llama/llama-3.1-8b-instruct:free",
            "deepseek/deepseek-r1:free",
        ]
        self.model = self.models[0]
        logger.info(f"✓ QA Agent initialized with {self.model}")

    # ─── SHARED HELPERS ──────────────────────────────────────────

    def _extract_excerpts_from_documents(self, question: str, documents: List[Dict], max_excerpts: int = 5) -> List[Dict]:
        excerpts = []
        query_terms = set(question.lower().split())

        for doc in documents:
            text = doc.get('text', '')
            sentences = re.split(r'[.!?]\s+', text)

            for sentence_idx, sentence in enumerate(sentences):
                if len(sentence.strip()) < 20:
                    continue
                sentence_lower = sentence.lower()
                matched_terms = sum(1 for t in query_terms if t in sentence_lower)
                relevance = matched_terms / len(query_terms) if query_terms else 0

                if relevance > 0.3:
                    char_position = text.find(sentence)
                    excerpts.append({
                        'document_id': doc['id'],
                        'filename': doc['filename'],
                        'text': sentence.strip(),
                        'relevance_score': round(relevance, 3),
                        'matched_terms': matched_terms,
                        'sentence_index': sentence_idx,
                        'char_position': char_position,
                        'length': len(sentence),
                        'context_before': text[max(0, char_position - 100):char_position] if char_position > 0 else '',
                        'context_after': text[char_position + len(sentence):char_position + len(sentence) + 100] if char_position >= 0 else ''
                    })

        excerpts.sort(key=lambda x: x['relevance_score'], reverse=True)
        return excerpts[:max_excerpts]

    def _calculate_answer_confidence(self, answer: str, documents: List[Dict], excerpts: List[Dict]) -> Dict:
        doc_coverage = min(len(documents) / 5, 1.0)
        avg_excerpt_relevance = sum(e['relevance_score'] for e in excerpts) / len(excerpts) if excerpts else 0
        citations_count = len(re.findall(r'\[Document \d+:', answer))
        citation_density = min(citations_count / 3, 1.0)
        answer_length = len(answer)
        length_score = 1.0 if 200 <= answer_length <= 2000 else (answer_length / 2000 if answer_length < 2000 else 0.5)

        overall = (doc_coverage * 0.25 + avg_excerpt_relevance * 0.35 + citation_density * 0.25 + length_score * 0.15) * 100

        return {
            'overall_confidence': round(overall, 2),
            'document_coverage_score': round(doc_coverage * 100, 2),
            'excerpt_quality_score': round(avg_excerpt_relevance * 100, 2),
            'citation_density_score': round(citation_density * 100, 2),
            'answer_length_score': round(length_score * 100, 2),
            'citations_found': citations_count,
            'answer_length_chars': answer_length,
            'confidence_level': 'high' if overall >= 75 else 'medium' if overall >= 50 else 'low'
        }

    def _parse_citations_from_answer(self, answer: str) -> List[Dict]:
        citations = []
        for match in re.finditer(r'\[Document (\d+): ([^\]]+)\]', answer):
            position = match.start()
            sentence_start = answer.rfind('.', 0, position) + 1
            sentence_end = answer.find('.', position)
            if sentence_end == -1:
                sentence_end = len(answer)
            citations.append({
                'document_id': int(match.group(1)),
                'filename': match.group(2),
                'position_in_answer': position,
                'cited_sentence': answer[sentence_start:sentence_end].strip(),
                'citation_index': len(citations) + 1
            })
        return citations

    def _detect_answer_type(self, question: str, answer: str) -> Dict:
        q = question.lower()
        question_types = []
        if any(w in q for w in ['what', 'which']): question_types.append('factual')
        if any(w in q for w in ['how', 'why']): question_types.append('explanatory')
        if any(w in q for w in ['when', 'where']): question_types.append('temporal_spatial')
        if any(w in q for w in ['compare', 'difference', 'versus']): question_types.append('comparative')
        if '?' in question: question_types.append('interrogative')

        return {
            'question_types': question_types or ['general'],
            'answer_structure': {
                'has_bullet_points': bool(re.search(r'[\n\r]\s*[•\-\*]', answer)),
                'has_numbered_list': bool(re.search(r'[\n\r]\s*\d+[\.\)]', answer)),
                'has_comparison': any(w in answer.lower() for w in ['whereas', 'however', 'compared to', 'while']),
                'has_quotes': '"' in answer or "'" in answer,
                'is_structured': bool(re.search(r'[\n\r]\s*[•\-\*]', answer)) or bool(re.search(r'[\n\r]\s*\d+[\.\)]', answer))
            }
        }

    def _generate_follow_up_questions(self, question: str, answer: str, documents: List[Dict]) -> List[str]:
        follow_ups = []
        doc_topics = {doc.get('filename', '').replace('.pdf', '').replace('.txt', '') for doc in documents}

        if len(documents) > 1:
            follow_ups.append(f"Can you compare the information across the {len(documents)} documents?")
        if 'what' in question.lower():
            follow_ups.append("How does this work in practice?")
            follow_ups.append("Why is this important?")
        elif 'how' in question.lower():
            follow_ups.append("What are the key steps involved?")
            follow_ups.append("Are there any alternatives?")

        for topic in list(doc_topics)[:2]:
            if topic:
                follow_ups.append(f"What else should I know about {topic}?")

        return follow_ups[:5]

    async def _call_llm(self, messages: List[Dict], max_tokens: int = 1500) -> Dict:
        """Call LLM with automatic model fallback"""
        last_error = None
        for model in self.models:
            try:
                logger.info(f"Trying model: {model}")
                response = await self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.3,
                    max_tokens=max_tokens,
                    timeout=30
                )
                logger.info(f"✓ Success with {model}")
                return {
                    'content': response.choices[0].message.content,
                    'model': model,
                    'tokens_used': response.usage.total_tokens
                }
            except Exception as e:
                logger.warning(f"✗ {model} failed: {str(e)[:100]}")
                last_error = str(e)
                continue

        return {
            'content': f"All models temporarily unavailable. Last error: {last_error}",
            'model': 'none',
            'tokens_used': 0
        }

    # ─── DOCUMENT RETRIEVAL ──────────────────────────────────────

    async def _retrieve_relevant_documents(self, question: str, top_k: int = 5) -> List[Dict]:
        try:
            results = await self.searcher.hybrid_search(question, limit=top_k)
            documents = []
            for idx, result in enumerate(results, 1):
                documents.append({
                    'id': idx,
                    'file_id': result.get('file_id'),
                    'filename': result.get('filename'),
                    'file_path': result.get('file_path'),
                    'text': result.get('text', '')[:4000],
                    'full_text_length': len(result.get('text', '')),
                    'relevance_score': result.get('rerank_score') or result.get('hybrid_info', {}).get('combined_score', 0),
                    'search_type': result.get('search_type', 'hybrid'),
                    'indexed_at': result.get('indexed_at', 'unknown')
                })
            logger.info(f"✓ Retrieved {len(documents)} documents")
            return documents
        except Exception as e:
            logger.error(f"Document retrieval failed: {e}")
            return []

    # ─── DOCUMENT-GROUNDED ANSWER ────────────────────────────────

    async def _generate_document_answer(self, question: str, documents: List[Dict]) -> Dict:
        context = "\n---\n".join([
            f"[Document {doc['id']}: {doc['filename']}]\n{doc['text']}"
            for doc in documents
        ])

        messages = [
            {
                "role": "system",
                "content": """You are a helpful assistant that answers questions based ONLY on provided documents.

RULES:
1. Answer ONLY using information from the provided documents
2. Cite sources inline as [Document X: filename]
3. Use MULTIPLE citations when info comes from multiple sources
4. If info is not in documents, say "I cannot find this in the provided documents"
5. Be comprehensive and include relevant details
6. If documents contradict each other, mention both perspectives with citations
7. Use bullet points or numbered lists when appropriate"""
            },
            {
                "role": "user",
                "content": f"""Answer this question using ONLY the provided documents:

Question: {question}

Documents:
{context}

Provide a detailed answer with inline citations."""
            }
        ]

        return await self._call_llm(messages)

    # ─── GENERAL LLM ANSWER ──────────────────────────────────────

    async def _generate_general_answer(self, question: str, documents: List[Dict] = None) -> Dict:
        if documents:
            context = "\n---\n".join([
                f"[Document {doc['id']}: {doc['filename']}]\n{doc['text']}"
                for doc in documents
            ])
            messages = [
                {
                    "role": "system",
                    "content": """You are a helpful, knowledgeable assistant.
    Answer using your general knowledge AND the provided documents when relevant.
    If you use document info, cite it as [Document X: filename].
    If documents aren't relevant to the question, just answer from general knowledge."""
                },
                {
                    "role": "user",
                    "content": f"Question: {question}\n\nRelevant documents (use if helpful):\n{context}"
                }
            ]
        else:
            messages = [
                {
                    "role": "system",
                    "content": """You are a helpful, knowledgeable assistant.
    Answer questions clearly and concisely using your general knowledge.
    Use bullet points or structure when it helps clarity."""
                },
                {
                    "role": "user",
                    "content": question
                }
            ]
        return await self._call_llm(messages)


    # ─── MAIN: answer_question ───────────────────────────────────

    async def answer_question(
        self,
        question: str,
        top_k: int = 5,
        return_sources: bool = True,
        include_excerpts: bool = True,
        mode: str = "document"   # "document" | "general"
    ) -> Dict:
        start_time = datetime.now()

        # ── GENERAL MODE ──────────────────────────────────────────
        if mode == "general":
            try:
                # Silently try to find relevant docs to enrich the answer
                used_docs = await self._retrieve_relevant_documents(question, top_k=3)
                answer_data = await self._generate_general_answer(question, used_docs if used_docs else None)
                processing_time = (datetime.now() - start_time).total_seconds()

                parsed_citations = self._parse_citations_from_answer(answer_data['content']) if used_docs else []

                sources = []
                for doc in used_docs:
                    doc_citations = [c for c in parsed_citations if c['document_id'] == doc['id']]
                    sources.append({
                        'document_id': doc['id'],
                        'filename': doc['filename'],
                        'file_path': doc['file_path'],
                        'file_id': doc['file_id'],
                        'relevance_score': round(doc['relevance_score'], 4),
                        'preview_url': f"/preview?file_path={doc['file_path']}",
                        'text_preview': doc['text'][:300] + "..." if len(doc['text']) > 300 else doc['text'],
                        'full_text_length': doc['full_text_length'],
                        'indexed_at': doc['indexed_at'],
                        'times_cited_in_answer': len(doc_citations),
                        'citation_details': doc_citations
                    })

                return {
                    'question': question,
                    'answer': answer_data['content'],
                    'mode': 'general',
                    'sources': sources,
                    'excerpts': [],
                    'citations': {
                        'total_citations': len(parsed_citations),
                        'unique_documents_cited': len(set(c['document_id'] for c in parsed_citations)),
                        'citation_details': parsed_citations
                    },
                    'confidence': None,
                    'answer_analysis': self._detect_answer_type(question, answer_data['content']),
                    'follow_up_questions': self._generate_follow_up_questions(question, answer_data['content'], used_docs),
                    'metadata': {
                        'documents_retrieved': len(used_docs),
                        'documents_cited': len(set(c['document_id'] for c in parsed_citations)),
                        'model': answer_data['model'],
                        'tokens_used': answer_data['tokens_used'],
                        'search_type': 'hybrid' if used_docs else 'none',
                        'processing_time_seconds': round(processing_time, 3),
                        'timestamp': datetime.now().isoformat()
                    }
                }
            except Exception as e:
                logger.error(f"General Q&A failed: {e}")
                return {'question': question, 'answer': f"Error: {str(e)}", 'mode': 'general', 'sources': [], 'metadata': {'error': str(e)}}


        # ── DOCUMENT MODE ─────────────────────────────────────────
        try:
            documents = await self._retrieve_relevant_documents(question, top_k)

            if not documents:
                return {
                    'question': question,
                    'answer': "No relevant documents found. Try switching to General mode for a knowledge-based answer.",
                    'mode': 'document',
                    'sources': [],
                    'excerpts': [],
                    'citations': {'total_citations': 0, 'unique_documents_cited': 0, 'citation_details': []},
                    'confidence': None,
                    'answer_analysis': {},
                    'follow_up_questions': [],
                    'metadata': {'documents_retrieved': 0, 'model': self.model, 'tokens_used': 0}
                }

            excerpts = self._extract_excerpts_from_documents(question, documents) if include_excerpts else []
            answer_data = await self._generate_document_answer(question, documents)
            answer = answer_data['content']

            confidence_metrics = self._calculate_answer_confidence(answer, documents, excerpts)
            parsed_citations = self._parse_citations_from_answer(answer)
            answer_analysis = self._detect_answer_type(question, answer)
            follow_up_questions = self._generate_follow_up_questions(question, answer, documents)

            sources = []
            for doc in documents:
                doc_citations = [c for c in parsed_citations if c['document_id'] == doc['id']]
                sources.append({
                    'document_id': doc['id'],
                    'filename': doc['filename'],
                    'file_path': doc['file_path'],
                    'file_id': doc['file_id'],
                    'relevance_score': round(doc['relevance_score'], 4),
                    'preview_url': f"/preview?file_path={doc['file_path']}",
                    'text_preview': doc['text'][:300] + "..." if len(doc['text']) > 300 else doc['text'],
                    'full_text_length': doc['full_text_length'],
                    'indexed_at': doc['indexed_at'],
                    'times_cited_in_answer': len(doc_citations),
                    'citation_details': doc_citations
                })

            processing_time = (datetime.now() - start_time).total_seconds()

            return {
                'question': question,
                'answer': answer,
                'mode': 'document',
                'sources': sources if return_sources else [],
                'excerpts': excerpts if include_excerpts else [],
                'citations': {
                    'total_citations': len(parsed_citations),
                    'unique_documents_cited': len(set(c['document_id'] for c in parsed_citations)),
                    'citation_details': parsed_citations
                },
                'confidence': confidence_metrics,
                'answer_analysis': answer_analysis,
                'follow_up_questions': follow_up_questions,
                'metadata': {
                    'documents_retrieved': len(documents),
                    'documents_cited': len(set(c['document_id'] for c in parsed_citations)),
                    'model': answer_data['model'],
                    'tokens_used': answer_data['tokens_used'],
                    'search_type': 'hybrid',
                    'processing_time_seconds': round(processing_time, 3),
                    'timestamp': datetime.now().isoformat()
                }
            }

        except Exception as e:
            logger.error(f"Document Q&A failed: {e}")
            return {'question': question, 'answer': f"Error: {str(e)}", 'mode': 'document', 'sources': [], 'metadata': {'error': str(e)}}

    # ─── MULTI-TURN CONVERSATION ─────────────────────────────────

    async def multi_turn_conversation(
        self,
        messages: List[Dict[str, str]],
        top_k: int = 5,
        mode: str = "document"
    ) -> Dict:
        try:
            last_question = next(
                (m['content'] for m in reversed(messages) if m.get('role') == 'user'),
                None
            )
            if not last_question:
                return {'error': 'No user question found'}

            # ── GENERAL MODE (Smart) ──────────────────────────────────
            if mode == "general":
                # Silently try to find relevant docs as bonus context
                used_docs = await self._retrieve_relevant_documents(last_question, top_k=3)

                if used_docs:
                    context = "\n---\n".join([
                        f"[Document {doc['id']}: {doc['filename']}]\n{doc['text']}"
                        for doc in used_docs
                    ])
                    system_msg = {
                        "role": "system",
                        "content": f"""You are a helpful assistant. Answer using your general knowledge.
    If the documents below are relevant to the question, use and cite them as [Document X: filename].
    If they are not relevant, ignore them and answer purely from general knowledge.

    Available Documents:
    {context}"""
                    }
                else:
                    system_msg = {
                        "role": "system",
                        "content": "You are a helpful assistant. Answer clearly and concisely using your general knowledge."
                    }

                result = await self._call_llm([system_msg] + messages)
                parsed_citations = self._parse_citations_from_answer(result['content']) if used_docs else []

                sources = [{
                    'document_id': doc['id'],
                    'filename': doc['filename'],
                    'file_path': doc['file_path'],
                    'relevance_score': round(doc['relevance_score'], 4),
                    'times_cited': len([c for c in parsed_citations if c['document_id'] == doc['id']])
                } for doc in used_docs]

                return {
                    'messages': messages + [{'role': 'assistant', 'content': result['content']}],
                    'mode': 'general',
                    'sources': sources,
                    'citations': {
                        'total_citations': len(parsed_citations),
                        'unique_documents_cited': len(set(c['document_id'] for c in parsed_citations)),
                        'citation_details': parsed_citations
                    },
                    'metadata': {
                        'tokens_used': result['tokens_used'],
                        'model': result['model'],
                        'documents_retrieved': len(used_docs),
                        'documents_cited': len(set(c['document_id'] for c in parsed_citations)),
                        'search_type': 'hybrid' if used_docs else 'none',
                        'timestamp': datetime.now().isoformat()
                    }
                }

            # ── DOCUMENT MODE ─────────────────────────────────────────
            documents = await self._retrieve_relevant_documents(last_question, top_k)

            if not documents:
                return {
                    'messages': messages + [{
                        'role': 'assistant',
                        'content': 'No relevant documents found. Try switching to General mode.'
                    }],
                    'mode': 'document',
                    'sources': [],
                    'citations': {
                        'total_citations': 0,
                        'unique_documents_cited': 0,
                        'citation_details': []
                    },
                    'metadata': {
                        'documents_retrieved': 0,
                        'documents_cited': 0,
                        'model': self.model,
                        'tokens_used': 0,
                        'search_type': 'hybrid',
                        'timestamp': datetime.now().isoformat()
                    }
                }

            context = "\n---\n".join([
                f"[Document {doc['id']}: {doc['filename']}]\n{doc['text']}"
                for doc in documents
            ])

            system_msg = {
                "role": "system",
                "content": f"""You are a helpful assistant answering questions based ONLY on the provided documents.
    Always cite sources as [Document X: filename].
    If the answer is not in the documents, say "I cannot find this in the provided documents".

    Available Documents:
    {context}"""
            }

            result = await self._call_llm([system_msg] + messages)
            assistant_message = {'role': 'assistant', 'content': result['content']}
            parsed_citations = self._parse_citations_from_answer(result['content'])

            sources = []
            for doc in documents:
                doc_citations = [c for c in parsed_citations if c['document_id'] == doc['id']]
                sources.append({
                    'document_id': doc['id'],
                    'filename': doc['filename'],
                    'file_path': doc['file_path'],
                    'relevance_score': round(doc['relevance_score'], 4),
                    'times_cited': len(doc_citations),
                    'citation_details': doc_citations
                })

            return {
                'messages': messages + [assistant_message],
                'mode': 'document',
                'sources': sources,
                'citations': {
                    'total_citations': len(parsed_citations),
                    'unique_documents_cited': len(set(c['document_id'] for c in parsed_citations)),
                    'citation_details': parsed_citations
                },
                'metadata': {
                    'tokens_used': result['tokens_used'],
                    'model': result['model'],
                    'documents_retrieved': len(documents),
                    'documents_cited': len(set(c['document_id'] for c in parsed_citations)),
                    'search_type': 'hybrid',
                    'timestamp': datetime.now().isoformat()
                }
            }

        except Exception as e:
            logger.error(f"Conversation failed: {e}")
            return {'error': str(e)}

