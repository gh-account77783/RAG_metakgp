import os
import httpx
from dotenv import load_dotenv

load_dotenv()

class LLMClient:
    def __init__(self):
        # API key for Ollama Cloud
        self.api_key = os.getenv("ollama_api_key") or os.getenv("OLLAMA_API_KEY")
        # Try both common base URLs if one fails
        self.base_urls = ["https://ollama.com", "https://api.ollama.com"]
        self.current_base_url = self.base_urls[0]

        if not self.api_key:
            print("Warning: OLLAMA_API_KEY not found in environment variables.")

    def _request(self, endpoint, payload):
        """Helper to send requests to Ollama Cloud, trying fallback base URLs."""
        for base_url in self.base_urls:
            url = f"{base_url}{endpoint}"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            try:
                with httpx.Client() as client:
                    response = client.post(url, headers=headers, json=payload, timeout=60.0)
                    if response.status_code == 200:
                        self.current_base_url = base_url
                        return response.json()
                    else:
                        print(f"Tried {url}: returned {response.status_code}")
            except Exception as e:
                print(f"Error connecting to {url}: {e}")

        raise Exception(f"Failed to get a successful response from all base URLs for {endpoint}")

    def list_models(self):
        """List available models on the cloud."""
        for base_url in self.base_urls:
            url = f"{base_url}/api/tags"
            headers = {"Authorization": f"Bearer {self.api_key}"}
            try:
                with httpx.Client() as client:
                    response = client.get(url, headers=headers, timeout=10.0)
                    if response.status_code == 200:
                        self.current_base_url = base_url
                        return response.json()
            except Exception as e:
                print(f"Error listing models from {url}: {e}")
        return None

    def generate(self, prompt, system_prompt="You are a helpful assistant."):
        """Generate a response from the Ollama Cloud API."""
        # We'll try common cloud model names if llama3 fails
        models_to_try = ["gemma4:31b-cloud"]

        for model in models_to_try:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                "stream": False
            }
            try:
                result = self._request("/api/chat", payload)
                return result['message']['content']
            except Exception as e:
                print(f"Model {model} failed: {e}")

        return "Error: All attempted models failed to generate a response."

if __name__ == "__main__":
    try:
        client = LLMClient()
        print(f"Testing with base URL: {client.current_base_url}")

        print("Listing models...")
        models = client.list_models()
        if models:
            print(f"Successfully listed models: {models}")
        else:
            print("Could not list models.")

        print("Testing generation...")
        res = client.generate("Hello! Are you working?")
        print(f"Test response: {res}")
    except Exception as e:
        print(f"Failed to initialize or test LLMClient: {e}")
