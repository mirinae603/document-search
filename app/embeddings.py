import logging
import requests
import numpy as np
from typing import List

logger = logging.getLogger(__name__)

class OpenRouterEmbeddings:
    def __init__(self, api_key: str, model: str = "openai/text-embedding-3-small"):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://openrouter.ai/api/v1"
        
        if not api_key:
            raise ValueError("OpenRouter API key required")
        
        logger.info(f"✓ OpenRouter initialized: {model}")
    
    async def embed_text(self, text: str) -> np.ndarray:
        """Generate embedding for text"""
        try:
            response = requests.post(
                f"{self.base_url}/embeddings",
                json={"model": self.model, "input": [text]},
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                timeout=30
            )
            
            if response.status_code != 200:
                raise Exception(f"API error: {response.status_code} - {response.text}")
            
            data = response.json()
            
            if 'data' not in data or len(data['data']) == 0:
                raise Exception(f"Invalid response: {data}")
            
            embedding = np.array(data['data'][0]['embedding'], dtype=np.float32)
            return embedding
        
        except Exception as e:
            logger.error(f"Embedding failed: {e}")
            raise
