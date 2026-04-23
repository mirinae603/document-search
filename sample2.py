# import requests
# import json

# response = requests.post(
#   url="https://openrouter.ai/api/v1/embeddings",
#   headers={
#     # "Authorization": "Bearer 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",
#     "Authorization": "Bearer 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",
#     "Content-Type": "application/json",
#     "HTTP-Referer": "<YOUR_SITE_URL>", # Optional. Site URL for rankings on openrouter.ai.
#     "X-OpenRouter-Title": "<YOUR_SITE_NAME>", # Optional. Site title for rankings on openrouter.ai.
#   },
#   data=json.dumps({
#     "model": "openai/text-embedding-3-small",
#     "input": "Your text string goes here",
#     # "input": ["text1", "text2", "text3"], # batch embeddings also supported!
#     "encoding_format": "float"
#   })
# )
#small change
# print(response.json())

import requests
import json


response = requests.post(
  url="https://openrouter.ai/api/v1/chat/completions",
  headers={
    "Authorization": "Bearer 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",
    "Content-Type": "application/json",
    "HTTP-Referer": "<YOUR_SITE_URL>", # Optional. Site URL for rankings on openrouter.ai.
    "X-OpenRouter-Title": "<YOUR_SITE_NAME>", # Optional. Site title for rankings on openrouter.ai.
  },
  data=json.dumps({
    "model": "openai/gpt-oss-20b:free",
    "messages": [
      {
        "role": "user",
        "content": "What is the meaning of life?"
      }
    ]
  })
)
print(response.json())