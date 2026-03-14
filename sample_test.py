# import requests
# import os

# OPENROUTER_API_KEY = "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"

# def test_openrouter_embeddings():
#     """Test OpenRouter embeddings API"""
    
#     print("Testing OpenRouter API...")
#     print(f"API Key: {OPENROUTER_API_KEY[:20]}..." if len(OPENROUTER_API_KEY) > 20 else "Not set")
    
#     url = "https://openrouter.ai/api/v1/embeddings"
    
#     headers = {
#         "Authorization": f"Bearer {OPENROUTER_API_KEY}",
#         "Content-Type": "application/json",
#         "HTTP-Referer": "http://localhost:8000",
#         "X-Title": "Document Search Test"
#     }
    

#     models_to_test = [
#         "openai/text-embedding-3-small",
#         "openai/text-embedding-ada-002",
#         "text-embedding-3-small",
#     ]
    
#     test_text = ["Hello world, this is a test."]
    
#     for model in models_to_test:
#         print(f"\n{'='*60}")
#         print(f"Testing model: {model}")
#         print('='*60)
        
#         payload = {
#             "model": model,
#             "input": test_text
#         }
        
#         try:
#             response = requests.post(url, json=payload, headers=headers, timeout=10)
            
#             print(f"Status Code: {response.status_code}")
#             print(f"Response: {response.text[:500]}")
            
#             if response.status_code == 200:
#                 data = response.json()
#                 if 'data' in data:
#                     print(f"✅ SUCCESS! Model '{model}' works!")
#                     print(f"Embedding dimensions: {len(data['data'][0]['embedding'])}")
#                     return model  # Return working model
#                 else:
#                     print(f"❌ No 'data' in response")
#             else:
#                 print(f"❌ Failed with status {response.status_code}")
        
#         except Exception as e:
#             print(f"❌ Error: {e}")
    
#     print("\n" + "="*60)
#     print("❌ No working embedding models found on OpenRouter")
#     print("Recommendation: Use local embeddings instead")
#     print("="*60)
#     return None

# if __name__ == "__main__":
#     working_model = test_openrouter_embeddings()
    
#     if working_model:
#         print(f"\n✅ Use this model in your config: {working_model}")
#     else:
#         print("\n💡 Switch to local embeddings:")
#         print("   pip install sentence-transformers")
#         print("   Update config.yaml: provider: 'local'")

from openai import OpenAI

client = OpenAI(
  base_url="https://openrouter.ai/api/v1",
  api_key="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",
)

completion = client.chat.completions.create(
  extra_headers={
  },
  extra_body={},
#   model="google/gemma-3-12b-it:free",
  model="stepfun/step-3.5-flash:free",
  messages=[
    {
      "role": "user",
      "content": "What is the meaning of life?"
    }
  ]
)
print(completion.choices[0].message.content)