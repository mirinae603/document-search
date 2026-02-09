import logging
import os
import requests
from typing import List, Dict
import yaml

logger = logging.getLogger(__name__)


class JinaReranker:
    """Jina AI Reranker"""
    
    def __init__(self, api_key: str, model: str = "jina-reranker-v2-base-multilingual"):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://api.jina.ai/v1/rerank"
        
        if not api_key:
            raise ValueError("Jina API key required")
        
        logger.info(f"✓ Jina Reranker initialized: {model}")
    
    def rerank(self, query: str, documents: List[str], top_n: int = 10) -> List[Dict]:
        """
        Rerank documents based on relevance to query
        
        Args:
            query: Search query
            documents: List of document texts
            top_n: Number of top results to return
            
        Returns:
            List of dicts with 'index', 'relevance_score', 'document'
        """
        try:
            if not documents:
                return []
            
            # Prepare request
            payload = {
                "model": self.model,
                "query": query,
                "documents": documents,
                "top_n": min(top_n, len(documents))
            }
            
            # Make API call
            response = requests.post(
                self.base_url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                timeout=30
            )
            
            if response.status_code != 200:
                logger.error(f"Jina API error: {response.status_code} - {response.text}")
                raise Exception(f"Jina API error: {response.status_code}")
            
            data = response.json()
            
            # Parse results - FIXED
            results = []
            
            # Check if 'results' exists in response
            if 'results' in data:
                for item in data['results']:
                    # Handle different response formats
                    if isinstance(item, dict):
                        results.append({
                            'index': item.get('index', 0),
                            'relevance_score': item.get('relevance_score', 0.0),
                            'document': documents[item.get('index', 0)] if item.get('index', 0) < len(documents) else ''
                        })
                    else:
                        logger.warning(f"Unexpected item format: {type(item)}")
            
            # Fallback: if no results or empty, return original order
            if not results:
                logger.warning("No results from Jina API, returning original order")
                results = [
                    {
                        'index': i,
                        'relevance_score': 1.0 - (i * 0.05),
                        'document': doc
                    }
                    for i, doc in enumerate(documents[:top_n])
                ]
            
            logger.info(f"✓ Reranked {len(results)} documents")
            return results
        
        except Exception as e:
            logger.error(f"Reranking failed: {e}")
            # Return original order if reranking fails
            return [
                {
                    'index': i,
                    'relevance_score': 1.0 - (i * 0.05),
                    'document': doc
                }
                for i, doc in enumerate(documents[:top_n])
            ]


class CohereReranker:
    """Cohere Reranker"""
    
    def __init__(self, api_key: str, model: str = "rerank-multilingual-v3.0"):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://api.cohere.ai/v1/rerank"
        
        if not api_key:
            raise ValueError("Cohere API key required")
        
        logger.info(f"✓ Cohere Reranker initialized: {model}")
    
    def rerank(self, query: str, documents: List[str], top_n: int = 10) -> List[Dict]:
        """Rerank documents using Cohere API"""
        try:
            if not documents:
                return []
            
            payload = {
                "model": self.model,
                "query": query,
                "documents": documents,
                "top_n": min(top_n, len(documents)),
                "return_documents": False  # We already have the documents
            }
            
            response = requests.post(
                self.base_url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                timeout=30
            )
            
            if response.status_code != 200:
                logger.error(f"Cohere API error: {response.status_code} - {response.text}")
                raise Exception(f"Cohere API error: {response.status_code}")
            
            data = response.json()
            
            results = []
            if 'results' in data:
                for item in data['results']:
                    if isinstance(item, dict):
                        idx = item.get('index', 0)
                        results.append({
                            'index': idx,
                            'relevance_score': item.get('relevance_score', 0.0),
                            'document': documents[idx] if idx < len(documents) else ''
                        })
            
            if not results:
                logger.warning("No results from Cohere API, returning original order")
                results = [
                    {
                        'index': i,
                        'relevance_score': 1.0 - (i * 0.05),
                        'document': doc
                    }
                    for i, doc in enumerate(documents[:top_n])
                ]
            
            logger.info(f"✓ Reranked {len(results)} documents")
            return results
        
        except Exception as e:
            logger.error(f"Reranking failed: {e}")
            return [
                {
                    'index': i,
                    'relevance_score': 1.0 - (i * 0.05),
                    'document': doc
                }
                for i, doc in enumerate(documents[:top_n])
            ]


class NoReranker:
    """Dummy reranker that returns original order"""
    
    def __init__(self):
        logger.info("✓ No reranker - using original ranking")
    
    def rerank(self, query: str, documents: List[str], top_n: int = 10) -> List[Dict]:
        """Return documents in original order"""
        return [
            {
                'index': i,
                'relevance_score': 1.0 - (i * 0.05),  # Fake declining scores
                'document': doc
            }
            for i, doc in enumerate(documents[:top_n])
        ]


def load_reranker_from_config(config_path: str = "./config/config.yaml"):
    """Load reranker based on config file"""
    try:
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
        
        reranker_config = config.get('reranker', {})
        provider = reranker_config.get('provider', 'none').lower()
        
        if provider == 'jina':
            jina_config = reranker_config.get('jina', {})
            api_key = jina_config.get('api_key') or os.getenv('JINA_API_KEY')
            model = jina_config.get('model', 'jina-reranker-v2-base-multilingual')
            return JinaReranker(api_key=api_key, model=model)
        
        elif provider == 'cohere':
            cohere_config = reranker_config.get('cohere', {})
            api_key = cohere_config.get('api_key') or os.getenv('COHERE_API_KEY')
            model = cohere_config.get('model', 'rerank-multilingual-v3.0')
            return CohereReranker(api_key=api_key, model=model)
        
        else:
            return NoReranker()
    
    except Exception as e:
        logger.error(f"Failed to load reranker config: {e}")
        return NoReranker()
